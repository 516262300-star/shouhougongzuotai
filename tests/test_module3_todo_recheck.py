from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AutomationPollState
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService
from aftersales_workbench.workflows.module3_todo_recheck import SCOPE, recheck_pending_todos
from tests import test_module3_unimported_refund as baseline
from tests.test_erp_unshipped_refund import ORDER_SN, _client


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


def seed(db, *, status="PENDING", attempts=0, origin="module3"):
    order, check, shop = baseline.seed(db)
    order.erp_sales_owner_status = "sales_not_found"
    check.payload = {"origin": "module3", "erp_refund_status": "blocked",
                     "erp_refund_message": "旧欠货差异"}
    todo = Task(after_sales_sn=order.after_sales_sn, action_type="ERP_CREATE_MANUAL_TODO",
                action_status=status, attempts=attempts, idempotency_key="recheck-todo",
                payload={"origin": origin})
    db.add(todo)
    db.commit()
    return order, check, todo, shop


def test_completed_erp_fact_clears_stale_todo_without_money_write(db):
    order, check, todo, _ = seed(db)
    client, state = _client(initially_completed=True)
    try:
        run = Module3ErpRefundService(db, client).run(limit=1, dry_run=False)
        assert run.todo_rechecks == 1
        assert run.applied == 0 and not state["write_called"]
        assert todo.action_status == "CANCELLED"
        assert check.action_status == "SUCCEEDED" and check.last_error is None
        assert order.workflow_status == "UNSHIPPED_AUTO_REFUNDED"
        assert recheck_pending_todos(Module3ErpRefundService(db, client)) == 0
    finally:
        client.close()


def test_ready_still_never_executes_in_recheck_and_obeys_cooldown(db):
    order, check, todo, _ = seed(db)
    client, state = _client()
    try:
        service = Module3ErpRefundService(db, client)
        assert recheck_pending_todos(service) == 1
        assert not state["write_called"]
        assert check.payload["erp_refund_status"] == "ready"
        assert order.workflow_status == "PENDING_CHECK"
        assert recheck_pending_todos(service) == 0
        assert db.get(AutomationPollState, (SCOPE, order.after_sales_sn)) is not None
    finally:
        client.close()


@pytest.mark.parametrize("change", [
    {"status": "SUCCEEDED"}, {"attempts": 1}, {"origin": "module1"},
])
def test_sent_attempted_other_module_are_not_rechecked(db, change):
    seed(db, **change)
    assert recheck_pending_todos(Module3ErpRefundService(db, None)) == 0


def test_other_platform_is_not_borrowing_pdd_checker(db):
    _, _, _, shop = seed(db)
    shop.platform = "TMALL"
    db.commit()
    assert recheck_pending_todos(Module3ErpRefundService(db, None)) == 0


def test_failure_keeps_todo_and_logs_retry_without_refund(db):
    order, check, todo, _ = seed(db)
    before = dict(check.payload)
    def fail(**kwargs):
        assert kwargs["reconcile_only"] is True
        raise ValueError("read failed")
    assert recheck_pending_todos(SimpleNamespace(session=db, run=fail)) == 1
    assert todo.action_status == "PENDING" and check.payload == before
    assert db.get(AutomationPollState, (SCOPE, order.after_sales_sn)).last_error


def test_dry_run_has_no_extra_rechecks_or_local_changes(db):
    _, check, todo, _ = seed(db)
    client, state = _client(initially_completed=True)
    try:
        run = Module3ErpRefundService(db, client).run(dry_run=True)
        assert run.todo_rechecks == 0
        assert check.action_status == todo.action_status == "PENDING"
        assert not state["write_called"]
        assert list(db.scalars(select(AutomationPollState))) == []
    finally:
        client.close()


def test_targeted_readonly_mode_does_not_recurse_or_execute(db):
    seed(db)
    client, state = _client()
    try:
        run = Module3ErpRefundService(db, client).run(
            platform_order_sn=ORDER_SN, dry_run=False, reconcile_only=True)
        assert run.todo_rechecks == run.applied == 0
        assert run.ready == 1 and not state["write_called"]
    finally:
        client.close()
