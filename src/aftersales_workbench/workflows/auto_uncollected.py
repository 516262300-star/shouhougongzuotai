"""使用物流接口的明确待揽收状态自动取证，不依赖人工确认或空轨迹推断。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.workflows.sync_safety import require_sync_safe_order
from aftersales_workbench.workflows.uncollected_refund import (
    order_snapshot,
    parse_confirmed_at,
    parse_shipping_time,
    utc,
)

EVIDENCE_KEY = "auto_uncollected_evidence"


def _require_order_and_notice(session, order):
    if (
        getattr(order, "logistics_physical_seen_at", None) is not None
        or getattr(order, "logistics_return_detected_at", None) is not None
        or order.after_sales_type != AfterSalesType.ONLY_REFUND
        or order.order_shipping_status != ShippingStatus.IN_TRANSIT
        or order.platform_order_amount != order.refund_amount
        or order.refund_amount <= 0
        or not order.items
        or order.workflow_status not in {
            WorkflowStatus.INTERCEPT_PUSHED, WorkflowStatus.INTERCEPT_CONFIRMED,
        }
        or order.refund_financial_status == "SUCCESS"
    ):
        raise ValueError("订单不满足已发货全额仅退款的自动待揽收条件")
    if session.scalar(select(Shop.platform).where(Shop.shop_id == order.shop_id)) != Platform.PDD:
        raise ValueError("自动待揽收退款目前仅支持拼多多")
    require_sync_safe_order(session, order.after_sales_sn)
    notice = session.scalar(select(AftersalesActionTask).where(
        AftersalesActionTask.after_sales_sn == order.after_sales_sn,
        AftersalesActionTask.action_type == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        AftersalesActionTask.action_status == AutomationTaskStatus.SUCCEEDED,
    ))
    if (
        notice is None
        or (notice.payload or {}).get("tracking_number") != order.forward_tracking_number
        or str((notice.payload or {}).get("carrier_code")) != str(order.carrier_code)
    ):
        raise ValueError("尚无该运单成功发送的拦截记录，不能自动退款")
    return notice


def build_auto_evidence(session, order, events, *, now: datetime) -> dict:
    from aftersales_workbench.workflows.module1_logistics import (
        LogisticsState,
        classify_logistics_trace,
    )

    if classify_logistics_trace(events) is not LogisticsState.UNCOLLECTED:
        raise ValueError("缺少物流接口明确待揽收状态")
    notice = _require_order_and_notice(session, order)
    latest = parse_shipping_time(events[0].time)
    times = [parse_shipping_time(event.time) for event in events]
    if any(left < right for left, right in zip(times, times[1:], strict=False)):
        raise ValueError("物流轨迹时间顺序异常，不能使用旧待揽收状态")
    if not timedelta(0) <= utc(now) - latest <= timedelta(hours=24):
        raise ValueError("待揽收记录时间异常或超过24小时，需继续核实物流")
    return {
        "version": 1, "source": "KUAIDI100_STATUS_CODE", "status_code": "102",
        "status_name": events[0].status_name, "context": events[0].context[:500],
        "event_at": latest.isoformat(), "checked_at": utc(now).isoformat(),
        "identity_verified": True, "snapshot": order_snapshot(order),
        "notice_task_id": notice.id,
    }


def require_auto_execution(session, order, task_id: int, settings, *, now=None) -> dict:
    from aftersales_workbench.workflows.module1_logistics import build_refund_business_hours

    now = utc(now or datetime.now(UTC))
    task = session.scalar(select(AftersalesActionTask).where(
        AftersalesActionTask.id == task_id,
        AftersalesActionTask.after_sales_sn == order.after_sales_sn,
        AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
    ).execution_options(populate_existing=True))
    if task is None or task.action_status != AutomationTaskStatus.RUNNING:
        raise ValueError("自动待揽收任务未取得执行权")
    payload = task.payload or {}
    evidence = payload.get(EVIDENCE_KEY) or {}
    if (
        payload.get("origin") != "module1" or payload.get("refund_gate") != "UNCOLLECTED"
        or evidence.get("version") != 1 or evidence.get("source") != "KUAIDI100_STATUS_CODE"
        or evidence.get("status_code") != "102" or evidence.get("identity_verified") is not True
        or order.logistics_state != "UNCOLLECTED"
        or evidence.get("snapshot") != order_snapshot(order)
    ):
        raise ValueError("自动待揽收证据缺失、失效或与订单不符")
    checked = parse_confirmed_at(evidence.get("checked_at", ""))
    event_at = parse_confirmed_at(evidence.get("event_at", ""))
    if (
        not timedelta(0) <= now - checked <= timedelta(seconds=90)
        or not timedelta(0) <= now - event_at <= timedelta(hours=24)
        or order.logistics_checked_at is None
        or utc(order.logistics_checked_at).replace(microsecond=0) != checked.replace(microsecond=0)
        or not build_refund_business_hours(settings).is_open(now)
    ):
        raise ValueError("自动待揽收复查已过期或不在客服工作时间")
    notice = _require_order_and_notice(session, order)
    if notice.id != evidence.get("notice_task_id"):
        raise ValueError("拦截发送记录已变化")
    return evidence


def validate_auto_shipping(info: dict, evidence: dict, *, now: datetime) -> None:
    shipped = parse_shipping_time(info.get("shipping_time"))
    if (
        not timedelta(0) <= utc(now) - shipped <= timedelta(hours=24)
        or shipped > parse_confirmed_at(evidence["checked_at"])
        or str(info.get("logistics_id")) != evidence["snapshot"]["carrier_code"]
    ):
        raise ValueError("平台不是24小时内已发货订单或快递公司已变更")
