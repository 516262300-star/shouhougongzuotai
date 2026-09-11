from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aftersales_workbench.api.routes.monitor import get_issue_service
from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AutomationActionType as Action,
)
from aftersales_workbench.db.models import (
    AutomationPollState,
    MarketplaceSyncIssue,
    MoneyOperation,
    ParcelNoticeRecord,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as Status,
)
from aftersales_workbench.main import app
from aftersales_workbench.services.runtime_issues import (
    RuntimeIssueCollector,
    RuntimeIssueService,
    observation,
    safe_text,
)
from tests import test_pdd_non_refund_sync as baseline
from tests import test_uncollected_refund as sample_data


class Collector:
    def __init__(self, rows):
        self.rows = rows

    def collect(self):
        return self.rows


def issue(**kwargs):
    return observation("task:1", "ERP", "ERP欠货表读取失败", **kwargs)


def journal(tmp_path, rows):
    collector = Collector(rows)
    return RuntimeIssueService(collector, tmp_path / "monitor.sqlite3", refresh_seconds=0)


def test_issue_survives_new_normal_cycle_missing_source_and_restart(tmp_path):
    service = journal(tmp_path, [issue(active=True)])
    assert service.list_issues()["counts"]["OPEN"] == 1
    service = journal(tmp_path, [])
    result = service.list_issues()
    assert result["counts"]["OPEN"] == 1
    assert result["items"][0]["reason"] == "ERP欠货表读取失败"
    assert result["items"][0]["resolved_at"] is None


def test_only_observed_errors_enter_history_then_explicit_recovery_reopens(tmp_path):
    service = journal(tmp_path, [issue(recovered=True)])
    assert service.list_issues(state="ALL")["items"] == []
    service.collector.rows = [issue(active=True)]
    first = service.list_issues()["items"][0]
    service.collector.rows = [issue()]  # 重试中不报错不代表已恢复。
    assert service.list_issues()["counts"]["OPEN"] == 1
    service.collector.rows = [issue(recovered=True)]
    result = service.list_issues(state="RESOLVED")
    assert result["counts"]["OPEN"] == 0
    assert result["items"][0]["resolved_at"]
    assert len(result["items"][0]["events"]) == 2
    service.collector.rows = [issue(active=True)]
    reopened = service.list_issues()["items"][0]
    assert reopened["first_seen_at"] == first["first_seen_at"]
    assert reopened["resolved_at"] is None
    assert len(reopened["events"]) == 3


def test_cancel_is_not_success_and_read_failure_does_not_clear_history(tmp_path):
    service = journal(tmp_path, [issue(active=True)])
    service.list_issues()

    class Broken:
        def collect(self):
            raise RuntimeError("database unavailable")

    service.collector = Broken()
    with pytest.raises(RuntimeError):
        service.list_issues()
    assert journal(tmp_path, []).list_issues()["counts"]["OPEN"] == 1
    service = journal(tmp_path, [issue(stopped=True)])
    result = service.list_issues(state="STOPPED")
    assert result["counts"] == {"OPEN": 0, "RESOLVED": 0, "STOPPED": 1}


def test_literal_search_shop_filters_pagination_and_no_duplicate_events(tmp_path):
    rows = [
        observation(
            f"task:{i}",
            "ERP",
            "failure",
            active=True,
            shop_id=i % 2 + 1,
            platform="PDD",
            platform_order_sn=f"order-{i}",
        )
        for i in range(20)
    ]
    service = journal(tmp_path, rows)
    first = service.list_issues(shop_id=1, page_size=3, page=2)
    assert first["pagination"]["total"] == 10
    assert len(first["items"]) == 3
    assert service.list_issues(keyword="order-19")["items"][0]["key"] == "task:19"
    assert service.list_issues(keyword="%_'")["items"] == []
    assert service.list_issues(platform="TMALL")["items"] == []
    assert all(len(i["events"]) == 1 for i in service.list_issues(page_size=100)["items"])


def test_redacts_credentials_and_urls():
    value = safe_text(
        "access_token=secret; client_secret:secret2 https://example.test/?password=secret3"
    )
    assert "secret" not in value.replace("client_secret", "")
    assert "example.test" not in value
    assert "private" not in safe_text(
        '{"access_token":"private"} Authorization: Bearer private.jwt'
    )


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


def collector(db, tmp_path):
    return RuntimeIssueCollector(
        db, SimpleNamespace(module1_desktop_ledger_path="ledger.jsonl"), tmp_path
    )


def test_erp_query_issue_links_exact_order_and_never_writes_business_state(db, tmp_path):
    order, _ = sample_data.sample.__wrapped__(db)
    task = Task(
        id=2,
        after_sales_sn=order.after_sales_sn,
        action_type=Action.ERP_CHECK_FULFILLMENT,
        action_status=Status.PENDING,
        idempotency_key="test-erp",
        attempts=0,
        last_error="ERP欠货表结构不完整",
        payload={"origin": "module3", "erp_refund_status": "unavailable"},
    )
    db.add(task)
    db.commit()
    service = RuntimeIssueService(
        collector(db, tmp_path), tmp_path / "history.sqlite3", refresh_seconds=0
    )
    result = service.list_issues()
    entry = next(i for i in result["items"] if i["key"] == "task:2")
    assert entry["platform_order_sn"] == "example-order" and entry["can_open_order"]
    assert entry["shop_id"] == 1
    assert task.action_status == Status.PENDING and task.attempts == 0
    assert not db.dirty and not db.new and not db.deleted
    assert not db.scalars(select(MoneyOperation)).all()
    task.payload = {"erp_refund_status": "not_required", "erp_refund_message": "核实无需补单"}
    task.last_error = None
    db.commit()
    assert service.list_issues(state="RESOLVED")["items"][0]["reason"] == "核实无需补单"
    assert task.action_status == Status.PENDING  # 监控不伪造动作执行成功。


def test_sync_issue_can_exist_without_order_and_same_refund_other_shop_distinct(db, tmp_path):
    sample_data.sample.__wrapped__(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    for shop_id in (1, 2):
        db.add(
            MarketplaceSyncIssue(
                shop_id=shop_id,
                after_sales_sn="not-synced",
                platform_order_sn="unknown-order",
                last_error="无法解析类型",
                attempts=1,
                checked_at=now,
                next_retry_at=now,
            )
        )
    db.commit()
    entries = [r for r in collector(db, tmp_path).collect() if r["key"].startswith("sync:")]
    assert len(entries) == 2 and entries[0]["key"] != entries[1]["key"]
    assert all(not r["can_open_order"] for r in entries)


def test_unknown_parcel_survives_deleted_or_succeeded_task(db, tmp_path):
    sample_data.sample.__wrapped__(db)
    db.add(
        ParcelNoticeRecord(
            parcel_key="p",
            carrier_code="384",
            tracking_number="JT-EXAMPLE",
            task_id=1,
            state="SendPressed",
            target_group="示例快递群",
            plan_hash="a" * 64,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()
    rows = collector(db, tmp_path).collect()
    assert next(r for r in rows if r["key"] == "task:1")["state"] == "OPEN"
    db.delete(db.get(Task, 1))
    db.commit()
    orphan = next(r for r in collector(db, tmp_path).collect() if r["key"] == "task:1")
    assert orphan["state"] == "OPEN" and orphan["target_group"] == "示例快递群"
    assert not orphan["can_open_order"]


def test_unknown_money_is_visible_independently_of_task(db, tmp_path):
    order, _ = sample_data.sample.__wrapped__(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    db.add(
        MoneyOperation(
            operation_key="example",
            platform="PDD",
            shop_id=1,
            after_sales_sn=order.after_sales_sn,
            operation_type="PLATFORM_REFUND",
            task_id=999,
            state="UNKNOWN",
            started_at=now,
            updated_at=now,
            last_error="request timeout",
        )
    )
    db.commit()
    row = next(r for r in collector(db, tmp_path).collect() if r["key"] == "money:example")
    assert row["state"] == "OPEN" and "不能直接重试退款" in row["suggestion"]


def test_migration_guard_and_normal_wait_are_not_new_execution_errors(db, tmp_path):
    order, _ = sample_data.sample.__wrapped__(db)
    order.workflow_status = "RETURN_WAITING_SCAN"
    now = datetime.now(UTC).replace(tzinfo=None)
    db.add(
        MoneyOperation(
            operation_key="legacy",
            platform="PDD",
            shop_id=1,
            after_sales_sn=order.after_sales_sn,
            operation_type="ERP_REFUND",
            state="UNKNOWN",
            started_at=now,
            updated_at=now,
            last_error="升级前资金任务：请求结果须只读核验，禁止因新账本为空而再次写入",
        )
    )
    db.add(
        AutomationPollState(
            scope="module2_erp",
            reference=order.after_sales_sn,
            checked_at=now,
            next_check_at=now,
            last_error="等待客户提供退货运单",
        )
    )
    db.commit()
    rows = collector(db, tmp_path).collect()
    assert next(r for r in rows if r["key"] == "money:legacy")["state"] is None
    assert next(r for r in rows if r["key"].startswith("poll:"))["state"] == "RESOLVED"
    assert db.get(MoneyOperation, "legacy").state == "UNKNOWN"  # 保护仍保留。
    order.logistics_state = "RETURNED"
    db.commit()
    assert (
        next(r for r in collector(db, tmp_path).collect() if r["key"].startswith("poll:"))["state"]
        == "OPEN"
    )


def test_issue_route_is_read_only_validated_and_sanitizes_failures(tmp_path):
    service = journal(tmp_path, [issue(active=True)])
    app.dependency_overrides[get_issue_service] = lambda: service
    try:
        client = TestClient(app)
        assert client.get("/api/v1/monitor/issues").json()["counts"]["OPEN"] == 1
        assert client.post("/api/v1/monitor/issues").status_code == 405
        for query in ("page=0", "page_size=101", "state=DELETE", "shop_id=-1", "category=ALL"):
            assert client.get(f"/api/v1/monitor/issues?{query}").status_code == 422

        class Broken:
            def collect(self):
                raise RuntimeError("mysql://user:secret@example")

        service.collector = Broken()
        response = client.get("/api/v1/monitor/issues")
        assert response.status_code == 503
        assert "secret" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_frontend_jump_clears_filters_and_has_no_money_write_controls():
    root = Path(__file__).resolve().parents[1]
    source = (root / "frontend/src/MonitorIssues.jsx").read_text(encoding="utf-8")
    app_source = (root / "frontend/src/App.jsx").read_text(encoding="utf-8")
    assert "onOpenOrder(item)" in source and "如何处理" in source
    assert 'method: "POST"' not in source
    assert 'started_on: "", ended_on: ""' in app_source
    assert "setSelected(item.after_sales_sn)" in app_source
