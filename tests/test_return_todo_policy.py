from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder
from aftersales_workbench.integrations.erp.return_match import ErpReturnMatchSyncService
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.services.return_todo_policy import (
    BALANCE_REASON,
    MANUAL_REASONS,
    check_balance_todo_before_publish,
    manual_balance_reason,
    match_snapshot,
    return_problem_state,
)
from aftersales_workbench.workflows.actions import ExternalActionExecutor
from aftersales_workbench.workflows.module1_manual_todo import Module1ManualTodoService
from tests.test_erp_closure_guard import Client, lookup, order, verify_closure
from tests.test_erp_closure_guard import db as db
from tests.test_manual_todo_control import settings
from tests.test_module1_manual_todo import FakeRepository, _candidate


def preflight(**changes):
    p = {
        "erp_match_status": "receivable_open",
        "erp_receivable_amount": "-3.29",
        "erp_refund_status": "blocked",
        "erp_refund_record_id": "record-1",
        "erp_refund_message": next(iter(MANUAL_REASONS)),
        "erp_refund_checked_at": datetime.now(UTC).isoformat(),
    }
    p.update(changes)
    p["erp_refund_match_snapshot"] = match_snapshot(p)
    return p


@pytest.mark.parametrize(
    "changes",
    [
        {"erp_refund_status": "ready"},
        {"erp_refund_status": "unavailable"},
        {"erp_refund_status": "not_found"},
        {"erp_refund_record_id": None},
        {"erp_refund_message": "ERP 客户累计应收不等于负的商家应收金额"},
        {"erp_refund_checked_at": (datetime.now(UTC) - timedelta(minutes=31)).isoformat()},
        {"erp_refund_checked_at": "invalid"},
        {"erp_match_status": "closed_loop"},
    ],
)
def test_balance_alone_waits_and_technical_errors_never_notify(changes):
    assert manual_balance_reason(preflight(**changes)) is None


def test_changed_return_cannot_reuse_old_preflight():
    p = preflight()
    assert manual_balance_reason(p)
    p["erp_receivable_amount"] = "-99"
    assert manual_balance_reason(p) is None


def test_service_skips_generic_balance_but_keeps_specific_failure():
    from aftersales_workbench.db.models import WorkflowStatus

    generic = _candidate(
        workflow=WorkflowStatus.RETURN_WAITING_ERP_MATCH,
        erp_match_payload={"erp_match_status": "receivable_open"},
    )
    required = _candidate(
        workflow=WorkflowStatus.RETURN_WAITING_ERP_MATCH, erp_match_payload=preflight()
    )
    repo = FakeRepository([generic, required])
    result = Module1ManualTodoService(repo).run(dry_run=False)
    assert result.skipped_accounting_verification == result.tasks_created == 1
    content = required.task_payload(started_at="now")["content"]
    assert "金额" in content and "客户累计应收" not in content


def prepare_db(db, *, settled=False, task_status="PENDING"):
    o = db.get(AfterSalesOrder, 1)
    o.platform_order_amount = o.refund_amount
    o.erp_sales_owner, o.erp_sales_owner_status = "测试业务员", "matched"
    t = db.get(Task, 2)
    t.action_status = task_status
    t.payload = {
        "origin": "module1",
        "reason_code": BALANCE_REASON,
        "erp_match_status": "receivable_open",
        "assignee": "测试业务员",
        "content": "原始已发消息",
        "marker": "test-marker",
        "started_at": "2026-09-01 12:00:00",
        "external_todo_id": "erp-todo-1",
    }
    if settled:
        proof = verify_closure(o, lookup(), Client())
        ErpReturnMatchSyncService.apply_lookup(db.get(Task, 1), o, proof, datetime.now(UTC))
    else:
        db.get(Task, 1).payload = {"erp_match_status": "receivable_open"}
    db.commit()
    return o, t


def test_sent_history_stays_intact_and_api_shows_resolved(db):
    o, t = prepare_db(db, settled=True, task_status="SUCCEEDED")
    before = deepcopy(t.payload)
    rows = AftersalesRecordService(db).list_manual_todos(page=1, page_size=10)["items"]
    row = next(r for r in rows if r["task_id"] == 2)
    assert row["problem_status"] == "RESOLVED" and row["sent_to_assignee"]
    assert row["content"] == "原始已发消息" and row["external_todo_id"] == "erp-todo-1"
    assert t.payload == before and t.action_status == "SUCCEEDED"
    assert not db.dirty and not db.new
    o.refund_financial_status = "FAILED"
    assert return_problem_state(t.payload, o, db.get(Task, 1))["problem_status"] != "RESOLVED"


@pytest.mark.parametrize("settled", [True, False])
def test_backlog_is_not_sent_or_retried_without_current_need(db, monkeypatch, settled):
    o, t = prepare_db(db, settled=settled)
    executor = ExternalActionExecutor(db, settings(erp_todo_publish_enabled=True))

    class Client:
        def create_todo(self, request):
            pytest.fail("不应向 ERP 发布旧余额提醒")

        def close(self):
            pass

    monkeypatch.setattr(executor, "_build_erp_todo_client", Client)
    result = executor.run(action_types=("ERP_CREATE_MANUAL_TODO",), dry_run=False)
    assert result.succeeded == result.failed == 0 and result.skipped == 1
    db.refresh(t)
    assert t.attempts == 0
    assert t.action_status == ("CANCELLED" if settled else "PENDING")


def test_shared_package_warning_is_never_resolved_by_balance(db):
    o, t = prepare_db(db, settled=True)
    payload = {**t.payload, "task_scope": "shared_package"}
    assert return_problem_state(payload, o, db.get(Task, 1)) == {}
    assert check_balance_todo_before_publish(db, payload, o.after_sales_sn)[0] == "ALLOW"


def test_stale_closed_label_without_refund_proof_not_resolved():
    o = order()
    o.workflow_status = "INTERCEPT_SUCCESS"
    t = SimpleNamespace(action_status="SUCCEEDED", payload={"erp_match_status": "closed_loop"})
    assert (
        return_problem_state({"origin": "module1", "reason_code": BALANCE_REASON}, o, t)[
            "problem_status"
        ]
        != "RESOLVED"
    )
