from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    Shop,
    WarehouseReturnItem,
    WarehouseReturnRecord,
)
from aftersales_workbench.workflows.module2_erp_intake import Module2ExceptionTodoService


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if isinstance(column.type, ENUM):
                column.type = String(100)
            elif isinstance(column.type, BigInteger):
                column.type = Integer()
            column.server_default = None
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Shop(shop_id=1, platform="PDD", shop_name="测试店", shop_code="pdd-test"))
        session.commit()
        yield session
    engine.dispose()


def add_return(db, number, *, task_status=None, refunded=False):
    order = AfterSalesOrder(
        id=number, shop_id=1, after_sales_sn=f"af-{number}",
        platform_order_sn=f"order-{number}", after_sales_type="RETURN_AND_REFUND",
        order_shipping_status="DELIVERED", workflow_status="RETURN_INSPECTED_FAIL",
        refund_amount=Decimal("10"), refund_financial_status="SUCCESS" if refunded else None,
        erp_sales_owner="测试业务员", erp_sales_owner_status="matched",
    )
    db.add(order)
    db.add(WarehouseReturnRecord(
        id=number, after_sales_sn=order.after_sales_sn, receipt_sn=f"return-{number}",
        return_tracking_number=f"tracking-{number}", destination="CUSTOMER_PROFILE",
        inspection_status="FAIL", operator="test", request_hash=f"hash-{number}",
        inspection_note="退货实收异常；少退或未收到：测试型号×1",
    ))
    if task_status:
        db.add(AftersalesActionTask(
            after_sales_sn=order.after_sales_sn, action_type="ERP_CREATE_MANUAL_TODO",
            action_status=task_status, attempts=0, payload={"origin": "module2"},
            idempotency_key=Module2ExceptionTodoService._idempotency_key(order),
        ))
    db.commit()
    return order


def test_existing_first_twenty_do_not_starve_new_rows_or_revive_cancelled(db):
    statuses = ["SUCCEEDED", "CANCELLED", "FAILED", "PENDING", "RUNNING"]
    for number in range(1, 26):
        add_return(db, number, task_status=statuses[(number - 1) % 5])
    for number in range(26, 29):
        add_return(db, number)
    service = Module2ExceptionTodoService(db)
    before = list(db.execute(select(AftersalesActionTask.__table__)).mappings())
    assert service.run(limit=2, dry_run=True).tasks_created == 2
    assert list(db.execute(select(AftersalesActionTask.__table__)).mappings()) == before
    assert service.run(limit=2, dry_run=False).tasks_created == 2
    assert service.run(limit=2, dry_run=False).tasks_created == 1
    assert service.run(limit=2, dry_run=False).scanned == 0
    assert list(db.execute(select(AftersalesActionTask.__table__).where(
        AftersalesActionTask.id <= 25,
    )).mappings()) == before


def test_cutoff_excludes_old_missing_todos_and_allows_new_missing_return(db):
    add_return(db, 1)
    add_return(db, 2, refunded=True)
    add_return(db, 3)
    service = Module2ExceptionTodoService(db)
    assert service.run(min_return_id=3, limit=1, dry_run=False).tasks_created == 1
    tasks = db.scalars(select(AftersalesActionTask)).all()
    assert [task.after_sales_sn for task in tasks] == ["af-3"]
    assert tasks[0].payload["assignee"] == "测试业务员"
    assert "少退或未收到" in tasks[0].payload["content"]
    assert service.run(min_return_id=3, dry_run=False).tasks_created == 0
    assert db.get(AfterSalesOrder, 1).workflow_status == "RETURN_INSPECTED_FAIL"


@pytest.mark.parametrize("field,value", [
    ("refund_financial_status", "SUCCESS"),
    ("platform_after_sales_status", 10),
])
def test_refund_appeal_keeps_separate_identity(db, field, value):
    order = add_return(db, 1, task_status="SUCCEEDED")
    setattr(order, field, value)
    db.commit()
    service = Module2ExceptionTodoService(db)
    assert service.run(dry_run=False).tasks_created == 1
    tasks = db.scalars(select(AftersalesActionTask).order_by(AftersalesActionTask.id)).all()
    assert tasks[0].action_status == "SUCCEEDED"
    assert tasks[1].idempotency_key.endswith(":ERP_CREATE_REFUND_APPEAL_TODO")
    assert service.run(dry_run=False).tasks_created == 0


def add_partial_return(db, *, task_status=None):
    order = add_return(db, 1, task_status=task_status)
    order.return_tracking_number = "tracking-1"
    order.items.append(AfterSalesItem(sku_code="sample-sku#铜本色", applied_quantity=51))
    receipt = db.get(WarehouseReturnRecord, 1)
    receipt.inspected_by = "系统ERP核对"
    receipt.items.append(WarehouseReturnItem(product_code="sample-sku", color="铜本色",
                                             quantity=27, item_status="NORMAL"))
    db.commit()
    return order, receipt


@pytest.mark.parametrize("refunded", [False, True])
def test_legacy_partial_failure_becomes_review_without_new_todo(db, refunded):
    order, receipt = add_partial_return(db)
    if refunded:
        order.platform_after_sales_status = 10
    db.commit()
    service = Module2ExceptionTodoService(db)
    preview = service.run(dry_run=True)
    assert preview.quantity_reviews == 1 and preview.tasks_created == 0
    assert order.workflow_status == "RETURN_INSPECTED_FAIL"
    applied = service.run(dry_run=False)
    assert applied.quantity_reviews == 1 and applied.tasks_created == 0
    assert order.workflow_status == "MANUAL_PROCESSING"
    assert len(order.exception_type) <= 50
    assert receipt.inspection_status == "FAIL"  # 历史审计保留，绝不改成通过。
    assert not db.scalars(select(AftersalesActionTask)).all()
    assert service.run(dry_run=False).scanned == 0


def test_old_successful_todo_does_not_create_partial_return_appeal(db):
    order, _ = add_partial_return(db, task_status="SUCCEEDED")
    order.platform_after_sales_status = 10
    db.commit()
    result = Module2ExceptionTodoService(db).run(dry_run=False)
    assert result.quantity_reviews == 1 and result.tasks_created == 0
    tasks = db.scalars(select(AftersalesActionTask)).all()
    assert len(tasks) == 1 and tasks[0].action_status == "SUCCEEDED"


def test_publisher_cancels_queued_false_shortage_without_contacting_erp(db, monkeypatch):
    from types import SimpleNamespace

    from aftersales_workbench.core.config import Settings
    from aftersales_workbench.db.models import AutomationActionType
    from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot

    order, _ = add_partial_return(db, task_status="PENDING")
    task = db.scalar(select(AftersalesActionTask))
    task.payload = {"origin": "module2", "reason_code": "RETURN_ITEM_MISMATCH",
                    "content": "历史少退提醒"}
    db.commit()
    snapshot = ExternalTaskSnapshot(task.id, order.after_sales_sn,
                                   AutomationActionType.ERP_CREATE_MANUAL_TODO, task.payload,
                                   order.platform_order_sn, "pdd-test")
    executor = ExternalActionExecutor(db, Settings(_env_file=None))
    monkeypatch.setattr(executor, "_list_pending", lambda *args: [snapshot])
    monkeypatch.setattr(executor, "_validate_write_gates", lambda *args: None)
    monkeypatch.setattr(executor, "_build_erp_todo_client", lambda: SimpleNamespace(
        close=lambda: None, create_todo=lambda *args: pytest.fail("不得发布错误待办")))
    result = executor.run(
        action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False)
    db.refresh(task)
    assert result.skipped == 1
    assert task.action_status == "CANCELLED" and task.attempts == 0
    assert task.payload["cancel_reason"] == "RETURN_QUANTITY_UNVERIFIED"
    assert task.payload["content"] == "历史少退提醒"
    assert order.workflow_status == "MANUAL_PROCESSING"
