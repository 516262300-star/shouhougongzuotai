"""平台交易完成后的有效仅退款，仍须按实际轨迹分流；全程模拟外部服务。"""

from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    AfterSalesType,
    Platform,
    Shop,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import LogisticsEvent
from aftersales_workbench.integrations.marketplace.issues import SyncIssueRepository
from aftersales_workbench.workflows.module1 import (
    Module1InterceptService,
    SqlAlchemyModule1Repository,
)
from aftersales_workbench.workflows.module1_manual_todo import (
    Module1ManualTodoService,
    SqlAlchemyModule1ManualTodoRepository,
)
from aftersales_workbench.workflows.module1_preflight import (
    Module1NotificationPreflightService,
    notification_preflight_ready,
)
from aftersales_workbench.workflows.pdd_completed_notice import verify_completed_notice
from aftersales_workbench.workflows.refund_preflight import verify_pdd_refund
from tests import test_pdd_non_refund_sync as base
from tests.test_uncollected_refund import Client


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture(autouse=True)
def live_client(monkeypatch):
    client = Client()
    client.info["order_status"] = 3
    monkeypatch.setattr(
        "aftersales_workbench.workflows.pdd_completed_notice.verify_completed_notice",
        lambda row: verify_completed_notice(row, client=client),
    )
    return client


@pytest.fixture
def order(db):
    db.add(
        Shop(
            shop_id=1,
            shop_code="pdd-example",
            shop_name="示例店铺",
            platform=Platform.PDD,
            is_active=1,
        )
    )
    row = AfterSalesOrder(
        shop_id=1,
        after_sales_sn="9001",
        platform_order_sn="example-order",
        after_sales_type=AfterSalesType.ONLY_REFUND,
        refund_amount=Decimal("18.47"),
        platform_order_amount=Decimal("18.47"),
        order_shipping_status="DELIVERED",
        workflow_status="PENDING_CHECK",
        forward_tracking_number="JT-EXAMPLE",
        carrier_code="384",
        platform_after_sales_status=2,
        platform_order_refund_status=2,
        refund_financial_status="PENDING",
        erp_sales_owner="示例业务员",
        erp_sales_owner_status="matched",
        items=[AfterSalesItem(sku_code="test-128#silver", applied_quantity=2)],
    )
    db.add(row)
    db.commit()
    return row


def create_notice(db):
    return Module1InterceptService(SqlAlchemyModule1Repository(db)).run(
        shop_codes=("pdd-example",),
        dry_run=False,
    )


def preflight(db, event=None, error=None):
    query = Mock(query=Mock(return_value=[event], side_effect=error))
    result = Module1NotificationPreflightService(
        db,
        query,
        carrier_map={"384": "jtexpress"},
    ).run(dry_run=False)
    task = db.scalar(
        select(AftersalesActionTask).where(
            AftersalesActionTask.action_type == "QYWX_INTERCEPT_NOTIFY"
        )
    )
    return result, task


@pytest.mark.parametrize(
    "status,context",
    [
        ("5", "快件正在派送中"),
        ("204", "包裹从驿站重新取出，继续派件"),
    ],
)
def test_completed_trade_with_delivery_trace_reaches_notice_once_without_funds(
    db, order, status, context
):
    assert create_notice(db).tasks_created == 1
    assert create_notice(db).tasks_created == 0
    result, task = preflight(db, LogisticsEvent(context=context, status_code=status))
    assert result.notices_ready == 1
    assert task.action_status == "PENDING"
    assert notification_preflight_ready(task.payload)
    assert task.payload["refund_gate"] == "HOLD"
    assert order.order_shipping_status == "DELIVERED"
    assert len(db.scalars(select(AftersalesActionTask)).all()) == 1
    client = Client()
    client.info["order_status"] = 3
    with pytest.raises(ValueError, match="在途"):
        verify_pdd_refund(client, order, origin="module1")
    assert client.writes == 0


def test_real_delivery_cancels_notice_and_queues_original_sales_owner_todo_once(db, order):
    assert create_notice(db).tasks_created == 1
    result, notice = preflight(db, LogisticsEvent(context="已签收，本人签收", status_code="3"))
    assert result.delivered_manual == 1
    assert notice.action_status == "CANCELLED"
    assert not notification_preflight_ready(notice.payload)
    assert order.workflow_status == "MANUAL_PROCESSING"
    service = Module1ManualTodoService(SqlAlchemyModule1ManualTodoRepository(db))
    first = service.run(dry_run=False)
    second = service.run(dry_run=False)
    assert first.tasks_created == 1 and second.tasks_created == 0
    todos = list(
        db.scalars(
            select(AftersalesActionTask).where(
                AftersalesActionTask.action_type == "ERP_CREATE_MANUAL_TODO"
            )
        )
    )
    assert len(todos) == 1
    assert todos[0].payload["assignee"] == "示例业务员"
    assert "已签收" in todos[0].payload["content"]
    assert len(db.scalars(select(AftersalesActionTask)).all()) == 2


def test_trace_query_failure_never_sends_or_assumes_delivered(db, order):
    create_notice(db)
    result, task = preflight(db, error=TimeoutError("carrier timeout"))
    assert result.logistics_query_failed == 1
    assert not notification_preflight_ready(task.payload)
    assert task.action_status == "PENDING"
    assert order.workflow_status == "PENDING_CHECK"


@pytest.mark.parametrize(
    "changes",
    [
        {"refund_amount": Decimal("1")},
        {"refund_amount": Decimal("0"), "platform_order_amount": Decimal("0")},
        {"after_sales_type": AfterSalesType.RETURN_AND_REFUND},
        {"platform_after_sales_status": 4},
        {"platform_after_sales_status": 10},
        {"platform_order_refund_status": 1},
        {"forward_tracking_number": ""},
        {"workflow_status": "MANUAL_PROCESSING"},
        {"order_shipping_status": "UNKNOWN"},
    ],
)
def test_completed_trade_extension_rejects_ineligible_records(db, order, changes):
    for key, value in changes.items():
        setattr(order, key, value)
    db.commit()
    assert create_notice(db).tasks_created == 0


def test_completed_trade_extension_preserves_shop_scope_and_sync_isolation(db, order):
    repo = SqlAlchemyModule1Repository(db)
    assert repo.list_candidates(shop_codes=("another-shop",), limit=20) == []
    SyncIssueRepository(db).record(
        1, "9001", "example sync issue", platform_order_sn="example-order"
    )
    db.commit()
    assert create_notice(db).tasks_created == 0


def test_completed_tmall_trade_is_not_enabled_by_pdd_extension(db, order):
    db.get(Shop, 1).platform = Platform.TMALL
    order.platform_after_sales_status_text = "WAIT_SELLER_AGREE"
    db.commit()
    assert (
        SqlAlchemyModule1Repository(db).list_candidates(
            shop_codes=None, limit=20, include_tmall=True
        )
        == []
    )


@pytest.mark.parametrize("change", ["closed", "return_refund", "partial", "order_refund_closed"])
def test_stale_local_application_never_creates_notice_or_signed_todo(
    db, order, live_client, change
):
    if change == "closed":
        live_client.detail["after_sales_status"] = 11
    elif change == "return_refund":
        live_client.detail["after_sales_type"] = 2
    elif change == "partial":
        live_client.detail["refund_amount"] = 100
    else:
        live_client.info["refund_status"] = 1
    assert create_notice(db).tasks_created == 0
    assert db.scalars(select(AftersalesActionTask)).all() == []
    assert order.workflow_status == "PENDING_CHECK"
    assert live_client.writes == 0


@pytest.mark.parametrize("change", ["identity", "tracking", "timeout", "missing_status"])
def test_realtime_check_error_holds_only_that_order(db, order, live_client, change):
    if change == "identity":
        live_client.detail["id"] = 9999
    elif change == "tracking":
        live_client.info["tracking_number"] = "OTHER-PARCEL"
    elif change == "timeout":
        live_client.get_order_information = Mock(side_effect=TimeoutError())
    else:
        del live_client.info["refund_status"]
    db.add(
        AfterSalesOrder(
            shop_id=1,
            after_sales_sn="9002",
            platform_order_sn="normal-order",
            after_sales_type=AfterSalesType.ONLY_REFUND,
            refund_amount=Decimal("1"),
            platform_order_amount=Decimal("1"),
            order_shipping_status="IN_TRANSIT",
            workflow_status="PENDING_CHECK",
            forward_tracking_number="NORMAL-PARCEL",
        )
    )
    db.commit()
    result = create_notice(db)
    assert result.tasks_created == 1
    assert result.completed_trade_check_errors[0]["after_sales_sn"] == "9001"
    assert db.scalar(select(AftersalesActionTask)).after_sales_sn == "9002"
    assert live_client.writes == 0
