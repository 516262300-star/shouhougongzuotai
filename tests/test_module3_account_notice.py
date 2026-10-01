from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AftersalesActionTask as Task, AutomationActionType
from aftersales_workbench.services.module3_todo_policy import (
    ACCOUNT_NOTICE_DELEGATED, account_balance_only,
)
from aftersales_workbench.workflows.actions import ExternalActionExecutor, WorkflowTransitionError
from aftersales_workbench.workflows.module3_exception_todo import SqlAlchemyModule3ExceptionTodoRepository
from tests import test_module3_unimported_refund as baseline
from tests import test_manual_todo_audit as audit_baseline


def balance_reason(amount="4.98"):
    return ("ERP“所有退货退款”已查到退款记录，待处理列表已无本单；"
            f"客户累计应收为 {amount}，尚未归零。平台退款与 ERP 核账分开确认，不重复退款或补单。")


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


@pytest.fixture
def records():
    yield from audit_baseline.records.__wrapped__()


def seed(db, reason=None, *, attempts=0, external=None):
    order, check, _ = baseline.seed(db)
    order.erp_sales_owner_status = "matched"
    order.erp_sales_owner = "测试业务员"
    reason = reason or balance_reason()
    check.payload = {**check.payload, "erp_refund_status": "not_found", "erp_refund_message": reason,
                     "erp_receivable_amount": "4.98"}
    todo = Task(after_sales_sn=order.after_sales_sn, action_type="ERP_CREATE_MANUAL_TODO",
                action_status="PENDING", attempts=attempts, idempotency_key="account-todo",
                payload={"origin": "module3", "reason_text": reason, "external_todo_id": external})
    db.add(todo)
    db.commit()
    return order, check, todo


@pytest.mark.parametrize("amount", ["4.98", "-427.29", "1", "-0.18"])
def test_balance_is_notification_policy_not_a_new_tolerance(amount):
    assert account_balance_only(balance_reason(amount))
    assert account_balance_only("【未发货退款核对：TEST-ORDER】 店铺：测试店；原因：" + balance_reason(amount)
                                + "；请核对商家应收、订单欠货和退款单状态。完整原因见售后工作台。")


@pytest.mark.parametrize("extra", ["本单仍有欠货；", "未匹配到本售后及金额对应的退款收款单；",
                                   "退款金额与商家应收不一致；"])
def test_mixed_refund_problems_are_still_candidates(db, extra):
    message = balance_reason().replace("客户累计应收", extra + "客户累计应收")
    assert not account_balance_only(message)
    _, _, todo = seed(db, message)
    repository = SqlAlchemyModule3ExceptionTodoRepository(db)
    assert len(repository.list_candidates(limit=1)) == 1
    assert repository.cancel_resolved(dry_run=False) == 0
    assert todo.action_status == "PENDING"


@pytest.mark.parametrize("last_error_only", [False, True])
def test_cancel_notice_preserves_order_balance_and_pending_verification(db, last_error_only):
    order, check, todo = seed(db)
    if last_error_only:
        check.payload = {k:v for k,v in check.payload.items() if k != "erp_refund_message"}
        check.last_error = balance_reason()
        db.commit()
    check_before = dict(check.payload)
    status_before = order.workflow_status
    repository = SqlAlchemyModule3ExceptionTodoRepository(db)
    assert repository.list_candidates(limit=1) == []
    assert repository.cancel_resolved(dry_run=True) == 1
    assert todo.action_status == "PENDING"
    assert repository.cancel_resolved(dry_run=False) == 1
    db.commit()
    assert todo.action_status == "CANCELLED"
    assert todo.payload["resolution_code"] == ACCOUNT_NOTICE_DELEGATED
    assert check.action_status == "PENDING" and check.payload == check_before
    assert order.workflow_status == status_before and order.refund_financial_status == "SUCCESS"
    # 后来出现真正的缺单问题，应恢复待办，不能沿用旧隐藏标记。
    check.payload = {**check.payload, "erp_refund_message": "未匹配到本售后及金额对应的退款收款单"}
    db.commit()
    candidate = repository.list_candidates(limit=1)[0]
    repository.enqueue_todo(candidate, started_at="2026-10-01 10:00:00", max_attempts=3)
    assert todo.action_status == "PENDING" and "resolution_code" not in todo.payload


@pytest.mark.parametrize("attempts,external", [(1, None), (1, "ERP-existing"), (0, "ERP-existing")])
def test_attempted_or_sent_audits_are_not_cancelled_or_republished(db, attempts, external):
    _, _, todo = seed(db, attempts=attempts, external=external)
    assert SqlAlchemyModule3ExceptionTodoRepository(db).cancel_resolved(dry_run=False) == 0
    executor = ExternalActionExecutor(db, Settings(_env_file=None))
    assert executor._list_pending((AutomationActionType.ERP_CREATE_MANUAL_TODO,), 1) == []
    assert todo.action_status == "PENDING" and todo.attempts == attempts
    assert todo.payload["external_todo_id"] == external


def test_pre_refund_balance_guard_stays_pending_but_does_not_notify(db):
    _, check, _ = seed(db, "ERP 客户累计应收不等于负的商家应收金额")
    check.payload = {**check.payload, "erp_refund_status": "blocked"}
    db.commit()
    assert SqlAlchemyModule3ExceptionTodoRepository(db).list_candidates(limit=1) == []
    assert check.action_status == "PENDING"


def test_old_notice_in_memory_cannot_reach_publisher():
    task = SimpleNamespace(payload={"origin": "module3", "reason_text": balance_reason()})
    with pytest.raises(WorkflowTransitionError, match="禁止重复发布"):
        ExternalActionExecutor._build_erp_todo_request(task)


@pytest.mark.parametrize("status", ["PENDING", "SUCCEEDED", "FAILED", "CANCELLED"])
@pytest.mark.parametrize("field", ["reason_text", "exception_message", "content"])
def test_hidden_in_list_search_and_counts_while_receipts_retained(records, status, field):
    service, db = records
    todo = db.get(Task, 1)
    todo.action_status = status
    todo.payload = {**todo.payload, "origin": "module3", field: balance_reason()}
    db.commit()
    before = list(db.execute(select(Task.__table__)).mappings())
    for filters in ({}, {"keyword": "after-order"}, {"origin": "module3"}, {"task_status": status}):
        page = service.list_manual_todos(page=1, page_size=20, **filters)
        assert all(x["source"] != "aftersales" for x in page["items"])
        assert page["summary"]["total"] == 5
    assert list(db.execute(select(Task.__table__)).mappings()) == before
    todo.payload = {**todo.payload, "reason_text": "退款金额与商家应收不一致"}
    db.commit()
    assert service.list_manual_todos(page=1, page_size=20, keyword="after-order")["pagination"]["total"] == 1


def test_other_module_balance_notices_are_not_silently_changed(records):
    service, db = records
    todo = db.get(Task, 1)
    todo.payload = {**todo.payload, "reason_text": balance_reason()}
    db.commit()
    assert service.list_manual_todos(page=1, page_size=20, keyword="after-order")["pagination"]["total"] == 1
