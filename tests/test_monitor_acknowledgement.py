import pytest
from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.monitor import get_issue_service
from aftersales_workbench.main import app
from aftersales_workbench.services.runtime_issues import observation
from tests.test_runtime_issues import journal


def row(reason="订单详情45001"):
    return observation(
        "sync:1:refund",
        "SYNC",
        reason,
        active=True,
        shop_id=1,
        after_sales_sn="refund",
        platform_order_sn="order",
        platform="PDD",
        can_open_order=False,
    )


def test_ack_persists_restarts_reopens_changed_reason_and_recovers(tmp_path):
    service = journal(tmp_path, [row()])
    item = service.list_issues()["items"][0]
    service.acknowledge(item["key"], item["revision"], "人工后台跟进退货")
    assert service.list_issues()["counts"]["OPEN"] == 0
    service = journal(tmp_path, [row()])
    item = service.list_issues(state="ACKNOWLEDGED")["items"][0]
    assert item["resolved_at"] is None
    assert item["acknowledgement_reason"] == "人工后台跟进退货"
    assert len(item["events"]) == 2
    service.collector.rows = [row("售后详情身份不一致")]
    item = service.list_issues()["items"][0]
    assert item["state"] == "OPEN"
    service.acknowledge(item["key"], item["revision"], "重新核验中")
    recovered = row()
    recovered.update(state="RESOLVED", reason="同步已恢复")
    service.collector.rows = [recovered]
    assert service.list_issues(state="RESOLVED")["counts"]["ACKNOWLEDGED"] == 0


def test_stale_revision_unknown_money_and_whole_shop_cannot_be_acknowledged(tmp_path):
    service = journal(tmp_path, [row()])
    old = service.list_issues()["items"][0]
    service.collector.rows = [row("新异常")]
    with pytest.raises(ValueError):
        service.acknowledge(old["key"], old["revision"], "已看过")
    for key in ("money:unknown", "shop:1", "task:99"):
        current = row()
        current["key"] = key
        service.collector.rows = [current]
        item = next(i for i in service.list_issues()["items"] if i["key"] == key)
        with pytest.raises(ValueError):
            service.acknowledge(key, item["revision"], "不允许隐藏")


def test_ack_route_local_origin_and_validation(tmp_path):
    service = journal(tmp_path, [row()])
    app.dependency_overrides[get_issue_service] = lambda: service
    try:
        client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
        item = service.list_issues()["items"][0]
        payload = dict(key=item["key"], expected_revision=item["revision"], reason="后台人工跟进")
        endpoint = "/api/v1/monitor/issues/acknowledge"
        assert client.post(endpoint, json=payload).status_code == 403
        headers = {"Origin": "http://127.0.0.1", "X-Workbench-Action": "acknowledge-sync-issue"}
        assert client.post(endpoint, json=payload, headers=headers).status_code == 200
        assert client.post(endpoint, json=payload, headers=headers).status_code == 409
        assert (
            client.get("/api/v1/monitor/issues?state=ACKNOWLEDGED").json()["counts"]["ACKNOWLEDGED"]
            == 1
        )
    finally:
        app.dependency_overrides.clear()


def test_only_fully_located_acknowledged_warning_changes_stage_label():
    import copy
    from types import SimpleNamespace

    from aftersales_workbench.api.routes.monitor import runtime_status

    source = {"modules": [{"stages": [{"id": "sync", "status": "warning", "error": "1笔异常"}]}]}
    monitor = SimpleNamespace(get_status=lambda: copy.deepcopy(source))
    for opened, missing, expected in ((0, 0, "acknowledged"), (1, 0, "warning"), (0, 1, "warning")):
        issues = SimpleNamespace(
            list_issues=lambda opened=opened, missing=missing, **kw: {
                "counts": {"OPEN": opened, "ACKNOWLEDGED": 1},
                "focus": {"issue_keys": ["sync:1:refund"], "unlocated_count": missing},
            }
        )
        result = runtime_status(monitor, issues)
        assert result["modules"][0]["stages"][0]["status"] == expected
    assert source["modules"][0]["stages"][0]["error"] == "1笔异常"


def test_ack_does_not_dismiss_sync_or_prevent_retry(tmp_path):
    from datetime import datetime, timedelta

    from aftersales_workbench.db.models import MarketplaceSyncIssue
    from aftersales_workbench.integrations.marketplace.issues import SyncIssueRepository
    from tests import test_pdd_non_refund_sync as baseline
    from tests.test_runtime_issues import collector

    dbs = baseline.db.__wrapped__()
    db = next(dbs)
    try:
        now = datetime.now() - timedelta(days=1)
        record = MarketplaceSyncIssue(
            shop_id=1,
            after_sales_sn="refund",
            platform_order_sn="order",
            last_error="45001",
            attempts=1,
            checked_at=now,
            next_retry_at=now,
        )
        db.add(record)
        db.commit()
        from aftersales_workbench.services.runtime_issues import RuntimeIssueService

        service = RuntimeIssueService(
            collector(db, tmp_path), tmp_path / "ack.sqlite3", refresh_seconds=0
        )
        item = next(i for i in service.list_issues()["items"] if i["key"] == "sync:1:refund")
        service.acknowledge(item["key"], item["revision"], "人工跟进")
        db.refresh(record)
        assert record.dismissed_at is None and record.resolved_at is None
        assert SyncIssueRepository(db).due(1) == ["refund"]
    finally:
        dbs.close()
