from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import AfterSalesOrder as Order
from aftersales_workbench.db.models import ShippingStatus as Shipping
from aftersales_workbench.db.models import Shop
from aftersales_workbench.integrations.tmall.mapper import normalize_refund
from aftersales_workbench.integrations.tmall.repository import SqlAlchemyTmallSyncRepository
from aftersales_workbench.integrations.tmall.shipping import classify_shipping, preserve_shipping
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.services.tmall_shipping_repair import repair_legacy_tmall_shipping
from aftersales_workbench.workflows.actions import (
    ActionCoordinator,
    ErpResultCode,
    WorkflowTransitionError,
)
from aftersales_workbench.workflows.module3 import SqlAlchemyModule3Repository
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        clone = table.to_metadata(metadata)
        for col in clone.columns:
            if isinstance(col.type, ENUM):
                col.type = String(100)
            elif isinstance(col.type, BigInteger):
                col.type = Integer()
            col.server_default = None
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                Shop(shop_id=1, platform="TMALL", shop_name="测试天猫", shop_code="tmall-test"),
                Shop(shop_id=2, platform="PDD", shop_name="测试拼多多", shop_code="pdd-test"),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def add_order(db, n=1, **changes):
    values = dict(
        id=n,
        shop_id=1,
        after_sales_sn=str(n),
        platform_order_sn=str(n + 100),
        after_sales_type="ONLY_REFUND",
        refund_amount=Decimal("2.00"),
        platform_order_amount=Decimal("2.00"),
        order_shipping_status="UNSHIPPED",
        platform_order_status_text="TRADE_CLOSED",
        refund_financial_status="SUCCESS",
        workflow_status="PENDING_CHECK",
        updated_at=datetime(2026, 9, 1),
    )
    row = Order(**{**values, **changes})
    db.add(row)
    db.commit()
    return row


def add_task(db, n=1, action="ERP_CHECK_FULFILLMENT", **changes):
    task = Task(
        **{
            "id": n,
            "after_sales_sn": str(n),
            "action_type": action,
            "action_status": "PENDING",
            "idempotency_key": f"test-{n}",
            "payload": {"origin": "module3"},
            **changes,
        }
    )
    db.add(task)
    db.commit()
    return task


@pytest.mark.parametrize(
    "status", [None, "", "TRADE_CLOSED", "TRADE_CLOSED_BY_TAOBAO", "new_state"]
)
def test_closed_or_unknown_never_default_unshipped(status):
    assert classify_shipping({"order_status": status}, {}) is Shipping.UNKNOWN


@pytest.mark.parametrize(
    "status", ["WAIT_SELLER_SEND_GOODS", "WAIT_BUYER_PAY", "TRADE_NO_CREATE_PAY"]
)
def test_explicit_not_shipped_status(status):
    assert classify_shipping({"order_status": status}, {"status": status}) is Shipping.UNSHIPPED
    assert (
        classify_shipping({"order_status": status}, {"status": "TRADE_CLOSED"}) is Shipping.UNKNOWN
    )


@pytest.mark.parametrize(
    "trade",
    [
        {"status": "WAIT_BUYER_CONFIRM_GOODS"},
        {"status": "SELLER_CONSIGNED_PART"},
        {"consign_time": "2026-09-01 12:00:00"},
        {"orders": {"order": [{"oid": "2", "consign_time": "2026-09-01 12:00:00"}]}},
        {"orders": {"order": [{"oid": "2", "status": "TRADE_FINISHED"}]}},
    ],
)
def test_closed_refund_cannot_hide_prior_shipping_or_shipped_sibling(trade):
    assert (
        classify_shipping({"oid": "1", "order_status": "TRADE_CLOSED"}, trade)
        is Shipping.IN_TRANSIT
    )


def test_signing_remains_delivered_and_bad_timestamp_not_unshipped():
    assert classify_shipping({"order_status": "TRADE_BUYER_SIGNED"}, {}) is Shipping.DELIVERED
    assert (
        classify_shipping(
            {"order_status": "WAIT_SELLER_SEND_GOODS"},
            {
                "consign_time": "0000-00-00 00:00:00",
            },
        )
        is Shipping.UNKNOWN
    )


def test_logistics_closed_or_absent_is_not_proof_of_never_shipping():
    def body(**row):
        return {"logistics_orders_get_response": {"shippings": {"shipping": [row]}}}

    refund = {"order_status": "TRADE_CLOSED"}
    assert (
        classify_shipping(refund, {}, body(status="CLOSED", seller_confirm="no"))
        is Shipping.UNKNOWN
    )
    assert (
        classify_shipping(refund, {}, body(status="CLOSED", seller_confirm="yes"))
        is Shipping.IN_TRANSIT
    )
    assert classify_shipping(refund, {}, body(out_sid="tracking")) is Shipping.UNKNOWN


@pytest.mark.parametrize(
    "previous,incoming,expected",
    [
        ("IN_TRANSIT", Shipping.UNKNOWN, Shipping.IN_TRANSIT),
        ("IN_TRANSIT", Shipping.UNSHIPPED, Shipping.IN_TRANSIT),
        ("DELIVERED", Shipping.IN_TRANSIT, Shipping.DELIVERED),
        ("DELIVERED", Shipping.UNKNOWN, Shipping.DELIVERED),
        ("PACKED_NOT_SHIPPED", Shipping.UNSHIPPED, Shipping.PACKED_NOT_SHIPPED),
        ("UNSHIPPED", Shipping.UNKNOWN, Shipping.UNKNOWN),
    ],
)
def test_preserve_shipping_is_not_regressive(previous, incoming, expected):
    assert preserve_shipping(previous, incoming) is expected


def test_repository_keeps_original_shipping_after_refund_closes(db):
    order = add_order(db, order_shipping_status="IN_TRANSIT")
    data = {
        "refund_id": "1",
        "tid": "101",
        "oid": "101",
        "refund_fee": "2.00",
        "payment": "2.00",
        "order_status": "TRADE_CLOSED",
        "status": "SUCCESS",
    }
    normalized = normalize_refund(data, data, {})
    assert normalized.order_shipping_status is Shipping.UNKNOWN
    SqlAlchemyTmallSyncRepository(db).upsert_refund(1, normalized)
    db.commit()
    assert order.order_shipping_status is Shipping.IN_TRANSIT


def test_new_module3_queue_excludes_old_wrong_unshipped_status(db):
    add_order(db, 1)
    add_order(db, 2, platform_order_status_text="WAIT_SELLER_SEND_GOODS")
    add_order(
        db,
        3,
        platform_order_status_text="WAIT_SELLER_SEND_GOODS",
        forward_tracking_number="tracking",
    )
    add_order(db, 4, order_shipping_status="UNKNOWN")
    add_order(db, 5, shop_id=2, platform_after_sales_status=10)
    candidates = SqlAlchemyModule3Repository(db).list_candidates(
        shop_codes=None,
        platform_order_sn=None,
        limit=20,
        include_tmall=True,
        tmall_min_order_id=1,
    )
    assert [r.after_sales_sn for r in candidates] == ["2", "5"]


def test_old_tmall_task_cannot_use_pdd_erp_executor(db):
    add_order(db, 1)
    add_task(db, 1)
    add_order(db, 2, shop_id=2, platform_after_sales_status=10)
    add_task(db, 2)
    rows = Module3ErpRefundService(db, None)._list_candidates(limit=20, platform_order_sn=None)
    assert [order.id for _, order in rows] == [2]
    assert (
        Module3ErpRefundService(db, None)._list_candidates(limit=20, platform_order_sn="101") == []
    )


@pytest.mark.parametrize(
    "action,result",
    [
        ("ERP_CHECK_FULFILLMENT", ErpResultCode.NOT_PACKED),
        ("ERP_CHECK_FULFILLMENT", ErpResultCode.PACKED_NOT_SHIPPED),
        ("ERP_CANCEL_UNSHIPPED_ORDER", ErpResultCode.COMPLETED),
        ("ERP_LOCK_PACKING", ErpResultCode.COMPLETED),
        ("ERP_CREATE_REFUND_RECORD", ErpResultCode.COMPLETED),
    ],
)
def test_erp_callback_cannot_bypass_shipping_guard(db, action, result):
    add_order(db)
    add_task(db, action=action)
    with pytest.raises(WorkflowTransitionError, match="发货事实未核实"):
        ActionCoordinator(db).confirm_erp_action(task_id=1, success=True, result_code=result)
    db.expire_all()
    assert db.get(Task, 1).action_status == "PENDING"
    assert db.query(Task).count() == 1
    assert db.get(Order, 1).workflow_status == "PENDING_CHECK"


def test_erp_callback_can_correct_shipped_fact_without_enqueueing_cancel(db):
    add_order(db)
    add_task(db)
    ActionCoordinator(db).confirm_erp_action(
        task_id=1, success=True, result_code=ErpResultCode.SHIPPED
    )
    assert db.get(Order, 1).order_shipping_status == Shipping.IN_TRANSIT
    assert db.query(Task).count() == 1


def test_legacy_repair_is_bounded_local_idempotent_and_preserves_tasks_and_money(db):
    add_order(db, 1)
    add_task(db, 1)
    add_order(db, 2, order_shipping_status="DELIVERED")
    add_order(db, 3, shop_id=2)
    add_order(db, 4, platform_order_status_text="WAIT_SELLER_SEND_GOODS")
    add_order(db, 5)
    assert repair_legacy_tmall_shipping(db, max_order_id=4)["eligible"] == 1
    assert db.get(Order, 1).order_shipping_status == "UNSHIPPED"
    assert repair_legacy_tmall_shipping(db, max_order_id=4, dry_run=False)["updated"] == 1
    db.expire_all()
    row = db.get(Order, 1)
    assert row.order_shipping_status == "UNKNOWN"
    assert row.updated_at == datetime(2026, 9, 1)
    assert row.workflow_status == "PENDING_CHECK" and row.refund_financial_status == "SUCCESS"
    assert row.platform_order_amount == Decimal("2.00")
    assert db.get(Order, 5).order_shipping_status == "UNSHIPPED"
    assert db.get(Task, 1).action_status == "PENDING" and db.query(Task).count() == 1
    assert repair_legacy_tmall_shipping(db, max_order_id=4, dry_run=False)["updated"] == 0
    result = AftersalesRecordService(db).get_order("1")
    assert "发货状态待核实" in result["decision"]["note"]
    assert result["platform_refund"]["status"] == "SUCCESS"
