from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    Shop,
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
