"""六个平台共用通知预检；测试通过不代表未接入平台已开启自动发群。"""

from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder as Order
from aftersales_workbench.db.models import AutomationActionType, Platform, Shop
from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100NoTraceError,
    LogisticsEvent,
)
from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot
from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticePlanner,
    DesktopNoticePreviewService,
)
from aftersales_workbench.workflows.module1_preflight import Module1NotificationPreflightService
from tests import test_pdd_non_refund_sync as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.mark.parametrize("platform", list(Platform))
@pytest.mark.parametrize("scenario", ["sixth_no_trace", "repeated_no_trace", "waiting_pickup",
                                      "network_error", "terminal_history"])
def test_platforms_share_no_trace_notice_policy_and_both_output_filters(db, platform, scenario):
    shop = Shop(shop_id=1, platform=platform, shop_code="synthetic-shop", shop_name="测试店",
                platform_shop_id="101", is_active=1)
    order = Order(id=1, shop_id=1, platform_order_sn="synthetic-order",
                  after_sales_sn="synthetic-refund", after_sales_type="ONLY_REFUND",
                  refund_amount=Decimal("10"), platform_order_amount=Decimal("10"),
                  forward_tracking_number="JT-SYNTHETIC", carrier_code="384",
                  order_shipping_status="IN_TRANSIT", workflow_status="PENDING_CHECK",
                  logistics_query_failures=20 if scenario == "repeated_no_trace" else 5)
    task = Task(id=1, after_sales_sn=order.after_sales_sn, action_type="QYWX_INTERCEPT_NOTIFY",
                action_status="PENDING", attempts=0, idempotency_key="synthetic-notice",
                payload={"platform": platform.value})
    if scenario == "terminal_history":
        order.logistics_state = "DELIVERED"
    db.add_all([shop, order, task])
    db.commit()
    query = Mock()
    if scenario == "waiting_pickup":
        query.query.return_value = [LogisticsEvent(context="包裹正在等待揽收")]
    else:
        query.query.side_effect = (TimeoutError("synthetic network error")
            if scenario == "network_error" else Kuaidi100NoTraceError("查询无结果"))
    result = Module1NotificationPreflightService(
        db, query, carrier_map={"384": "jtexpress"}).run(dry_run=False)
    planner = DesktopNoticePlanner({"384": "测试极兔群"}, {"384": "jtexpress"})
    preview = DesktopNoticePreviewService(db, planner).run()
    snapshot = ExternalTaskSnapshot(task.id, task.after_sales_sn,
        AutomationActionType.QYWX_INTERCEPT_NOTIFY, task.payload,
        order.platform_order_sn, shop.shop_code)
    ready, blocked = ExternalActionExecutor._filter_notification_preflight([snapshot])
    allowed = scenario not in {"network_error", "terminal_history"}
    assert result.notices_ready == preview.ready == len(ready) == int(allowed)
    assert preview.blocked_preflight == blocked == int(not allowed)
    assert task.action_status == "PENDING" and task.attempts == 0
    assert task.payload["refund_gate"] == "HOLD"
    assert order.workflow_status == "PENDING_CHECK"
    assert order.logistics_state == ("DELIVERED" if scenario == "terminal_history" else "UNKNOWN")
    assert list(db.scalars(select(Task))) == [task]  # 不产生退款、重复通知或人工待办。
