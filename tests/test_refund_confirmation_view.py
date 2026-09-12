from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AutomationPollState
from aftersales_workbench.services.refund_confirmation_view import refund_cycle_view
from aftersales_workbench.services.runtime_issues import RuntimeIssueCollector
from aftersales_workbench.services.runtime_monitor import RuntimeMonitorService
from tests.test_pdd_money_reconciliation import setup
from tests.test_pdd_non_refund_sync import db as base_db


@pytest.fixture
def db():
    yield from base_db.__wrapped__()


def cycle(task_id):
    return {
        "ok": False,
        "pdd_refund": {
            "status": "failed",
            "failed": 1,
            "failed_task_ids": [task_id],
            "error": "拼多多退款执行失败 1 笔",
        },
    }


@pytest.mark.parametrize("state", ["UNKNOWN", "REQUEST_STARTED"])
def test_pending_is_not_failure_or_success_and_view_never_writes(db, monkeypatch, state):
    order, task, money, _, _ = setup(db, monkeypatch, "FAILED", state)
    source = cycle(task.id)
    original = deepcopy(source)

    def read_only(conn, cursor, statement, *args):
        assert not statement.lstrip().lower().startswith(("update", "insert", "delete"))

    event.listen(db.get_bind(), "before_cursor_execute", read_only)
    view = refund_cycle_view(db, source)
    stage = view["pdd_refund"]
    assert stage["status"] == "awaiting_confirmation" and stage["failed"] == 0
    assert stage["pending_confirmation"] == 1 and stage["confirmed_after_query"] == 0
    assert stage["execution_failed_task_ids"] == [task.id] and stage["failed_task_ids"] == []
    assert view["ok"] is True and source == original
    assert task.action_status == "FAILED" and money.state == state


def test_confirmed_refund_clears_display_before_next_worker_cycle(db, monkeypatch):
    _, task, money, _, _ = setup(db, monkeypatch, "SUCCEEDED", "CONFIRMED")
    view = refund_cycle_view(db, cycle(task.id))
    assert view["pdd_refund"]["status"] == "completed"
    assert view["pdd_refund"]["confirmed_after_query"] == 1
    assert view["pdd_refund"]["failed"] == 0


def test_mixed_failure_keeps_actual_failure_and_other_module_failure(db, monkeypatch):
    _, task, _, _, _ = setup(db, monkeypatch, "FAILED")
    source = cycle(task.id)
    source["pdd_refund"].update(failed=2, failed_task_ids=[task.id, 999])
    source["notification"] = {"status": "failed", "error": "发送故障"}
    view = refund_cycle_view(db, source)
    assert view["pdd_refund"]["failed_task_ids"] == [999]
    assert view["pdd_refund"]["status"] == "failed" and view["ok"] is False
    assert "失败 1 笔" in view["pdd_refund"]["error"]
    assert view["notification"] == source["notification"]


@pytest.mark.parametrize("change", ["shop", "order", "type", "task", "ids", "module", "key"])
def test_unreliable_identity_or_failure_list_never_clears_failure(db, monkeypatch, change):
    _, task, money, _, _ = setup(db, monkeypatch, "FAILED")
    source = cycle(task.id)
    if change == "shop":
        money.shop_id = 99
    if change == "order":
        money.snapshot = {"platform_order_sn": "other"}
    if change == "type":
        money.operation_type = "ERP_REFUND"
    if change == "task":
        money.task_id = 999
    if change == "ids":
        source["pdd_refund"]["failed_task_ids"] = [True]
    if change == "module":
        task.payload = {"origin": "module2"}
    if change == "key":
        money.operation_key = "wrong-key"
    db.commit()
    view = refund_cycle_view(db, source)
    assert view["pdd_refund"]["status"] == "failed" and view["pdd_refund"]["failed"] == 1
    assert view["ok"] is False


def test_pending_persists_across_empty_cycles_and_module2_is_separate(db, monkeypatch):
    _, task, _, _, _ = setup(db, monkeypatch, "FAILED")
    task.payload = {"origin": "module2"}
    db.commit()
    source = {
        "ok": True,
        "pdd_refund": {"status": "completed", "failed": 0},
        "module2_pdd_refunds": {"status": "completed", "failed": 0},
    }
    view = refund_cycle_view(db, source)
    assert view["pdd_refund"]["pending_confirmation"] == 0
    assert view["module2_pdd_refunds"]["status"] == "awaiting_confirmation"


def test_issue_labels_and_next_read_check_then_recovery(db, monkeypatch, tmp_path):
    _, task, money, _, _ = setup(db, monkeypatch, "FAILED")
    db.add(
        AutomationPollState(
            scope="pdd_failed_refund",
            reference="123",
            checked_at=datetime(2026, 9, 12),
            next_check_at=datetime(2026, 9, 12, 0, 5),
        )
    )
    db.commit()
    collector = RuntimeIssueCollector(db, Settings(), tmp_path)
    row = next(r for r in collector.collect() if r["key"] == f"task:{task.id}")
    assert row["pending_confirmation"] and row["state"] == "OPEN"
    assert "退款结果待确认" in row["reason"] and row["next_check_at"]
    task.action_status, task.last_error, money.state = "SUCCEEDED", None, "CONFIRMED"
    db.commit()
    rows = collector.collect()
    assert all(
        r["state"] == "RESOLVED" and not r["pending_confirmation"]
        for r in rows
        if r["key"] in {f"task:{task.id}", f"money:{money.operation_key}"}
    )


def test_running_monitor_explains_pending_without_recent_failure_banner(db, monkeypatch, tmp_path):
    import json

    from aftersales_workbench.services import runtime_monitor as module

    _, task, _, _, _ = setup(db, monkeypatch, "FAILED")
    source = cycle(task.id)
    source.update(
        started_at=datetime.now(UTC).isoformat(), finished_at=datetime.now(UTC).isoformat()
    )
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "module1-worker.pid").write_text("123")
    (runtime / "module1-worker.log").write_text(json.dumps(source))
    monkeypatch.setattr(module, "_pid_is_running", lambda _: True)
    service = RuntimeMonitorService(db, Settings(), tmp_path)
    monkeypatch.setattr(service, "_desktop_notification_recovery", lambda: {})
    status = service.get_status()
    assert status["state"] == "pending" and "待确认" in status["state_label"]
    assert status["worker"]["last_cycle_ok"] is True
    assert status["worker"]["last_cycle_execution_ok"] is False
    assert status["modules"][0]["status"] == "pending"
