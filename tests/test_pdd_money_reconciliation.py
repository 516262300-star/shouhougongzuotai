from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aftersales_workbench.db.models import AftersalesActionTask, MoneyOperation
from aftersales_workbench.workflows import pdd_reconciliation as module
from aftersales_workbench.workflows.money_operations import operation_key, run_money_write
from tests.test_pdd_refund_cases import db as base_db
from tests.test_pdd_refund_cases import seed
from tests.test_pdd_sync import _shop


@pytest.fixture
def db():
    yield from base_db.__wrapped__()


def setup(db, monkeypatch, task_state="SUCCEEDED", money_state="UNKNOWN"):
    order, task, notice, client = seed(db)
    task.action_status = task_state
    if task_state == "SUCCEEDED":
        order.refund_financial_status = "SUCCESS"
        order.actual_refund_amount = Decimal("1")
        order.workflow_status = "INTERCEPT_SUCCESS"
        task.last_error = None
    client.details["123"]["after_sales_status"] = 10
    client.details["123"]["refund_amount"] = 100
    operation = MoneyOperation(
        operation_key=operation_key("PDD", order.shop_id, "123", "PLATFORM_REFUND"),
        platform="PDD",
        shop_id=order.shop_id,
        after_sales_sn="123",
        operation_type="PLATFORM_REFUND",
        task_id=task.id,
        state=money_state,
        started_at=datetime(2026, 9, 12),
        updated_at=datetime(2026, 9, 12),
        last_error="响应结果未知",
        snapshot={"platform_order_sn": "order-1", "refund_amount": "1.00"},
    )
    db.add(operation)
    db.commit()
    monkeypatch.setattr(module, "load_configured_pdd_shops", lambda *a, **kw: [_shop()])
    service = module.PddFailedRefundReconciler(db, None, client_factory=lambda _: client)
    return order, task, operation, client, service


@pytest.mark.parametrize("task_state", ["FAILED", "SUCCEEDED"])
@pytest.mark.parametrize("money_state", ["UNKNOWN", "REQUEST_STARTED"])
def test_live_success_closes_task_and_original_money_together(
    db, monkeypatch, task_state, money_state
):
    order, task, operation, client, service = setup(db, monkeypatch, task_state, money_state)
    before_tasks = db.scalar(select(func.count()).select_from(AftersalesActionTask))
    result = service.run(dry_run=False, after_sales_sns=["123"])
    assert result["confirmed_success"] == 1 and result["unavailable"] == 0
    assert task.action_status == "SUCCEEDED" and task.attempts == 1
    assert operation.state == "CONFIRMED" and operation.last_error is None
    proof = operation.snapshot["readonly_confirmation"]
    assert proof["previous_state"] == money_state and proof["refund_amount"] == "1"
    assert proof["previous_error"] == "响应结果未知"
    if task_state == "SUCCEEDED":
        assert order.workflow_status == "INTERCEPT_SUCCESS"
        assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == before_tasks
    assert service.run(dry_run=False, after_sales_sns=["123"])["scanned"] == 0
    # 已确认仍永久占用资金键，不因显示恢复而允许第二次退款。
    with pytest.raises(ValueError, match="已经发起"):
        run_money_write(
            db,
            order,
            operation_type="PLATFORM_REFUND",
            task_id=task.id,
            write=lambda: pytest.fail("不能重发退款"),
        )


def test_preview_keeps_money_and_order_unchanged(db, monkeypatch):
    order, task, operation, client, service = setup(db, monkeypatch)
    assert service.run(dry_run=True, after_sales_sns=["123"])["confirmed_success"] == 1
    assert operation.state == "UNKNOWN" and "readonly_confirmation" not in operation.snapshot
    assert order.workflow_status == "INTERCEPT_SUCCESS"


@pytest.mark.parametrize("change", ["order", "aftersale", "amount", "pending", "snapshot", "task"])
def test_inconsistent_or_pending_fact_keeps_unknown_money(db, monkeypatch, change):
    order, task, operation, client, service = setup(db, monkeypatch)
    detail = client.details["123"]
    if change == "order":
        detail["order_sn"] = "other"
    elif change == "aftersale":
        detail["id"] = 456
    elif change == "amount":
        detail["refund_amount"] = 200
    elif change == "pending":
        detail["after_sales_status"] = 2
    elif change == "snapshot":
        operation.snapshot = {"platform_order_sn": "other", "refund_amount": "1.00"}
    else:
        operation.task_id = task.id + 100
    db.commit()
    result = service.run(dry_run=False, after_sales_sns=["123"])
    assert result["confirmed_success"] == 0 and operation.state == "UNKNOWN"
    assert task.action_status == "SUCCEEDED" and task.attempts == 1
    assert order.refund_financial_status == "SUCCESS"


def test_erp_money_is_not_included_in_platform_confirmation(db, monkeypatch):
    order, task, operation, client, service = setup(db, monkeypatch)
    operation.operation_type = "ERP_REFUND"
    db.commit()
    assert service.run(dry_run=False, after_sales_sns=["123"])["scanned"] == 0
    assert operation.state == "UNKNOWN" and client.calls == []


def test_read_failure_does_not_confirm_or_retry_refund(db, monkeypatch):
    order, task, operation, client, service = setup(db, monkeypatch)

    def fail(**kwargs):
        raise TimeoutError("read timeout")

    client.get_refund_information = fail
    assert service.run(dry_run=False, after_sales_sns=["123"])["unavailable"] == 1
    assert operation.state == "UNKNOWN" and task.action_status == "SUCCEEDED"


def test_task_failure_rolls_back_money_confirmation_in_same_transaction(db, monkeypatch):
    order, task, operation, client, service = setup(db, monkeypatch, "FAILED")
    def fail(*args):
        raise RuntimeError("local transition failed")
    service.apply_observation = fail
    assert service.run(dry_run=False, after_sales_sns=["123"])["unavailable"] == 1
    assert operation.state == "UNKNOWN" and task.action_status == "FAILED"
