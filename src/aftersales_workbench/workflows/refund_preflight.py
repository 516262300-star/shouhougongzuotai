"""不可逆写入前的只读平台核验。任何缺字段、变更或未知状态均拒绝写入。"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from aftersales_workbench.db.models import (
    RECORD_ONLY_AFTERSALES_TYPES,
    AfterSalesType,
    ShippingStatus,
)
from aftersales_workbench.integrations.pdd.mapper import normalize_refund, unwrap_order_information
from aftersales_workbench.workflows.uncollected_refund import validate_shipping_snapshot


def verify_pdd_refund(
    client, order, *, origin: str, uncollected_confirmation=None, auto_uncollected_evidence=None,
    no_trace_risk_evidence=None, now=None,
) -> bool:
    """返回 True 表示平台明确已退款；此函数绝不调用写接口。"""
    now = now or datetime.now(UTC)
    if order.after_sales_type in RECORD_ONLY_AFTERSALES_TYPES:
        raise ValueError("补寄/维修不属于退款业务，禁止执行退款任务")
    detail = client.get_refund_information(
        order_sn=order.platform_order_sn,
        after_sales_id=int(order.after_sales_sn),
    )
    if (
        str(detail.get("id") or "") != order.after_sales_sn
        or str(detail.get("order_sn") or "") != order.platform_order_sn
    ):
        raise ValueError("拼多多退款详情身份不一致，禁止自动退款")
    try:
        status = int(detail["after_sales_status"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("拼多多退款详情缺少有效状态，禁止自动退款") from exc
    if str(detail.get("after_sales_type") or "") in {"4", "5"}:
        raise ValueError("平台最新售后为补寄/维修，不能认作退款成功或执行退款")
    allowed = {2} if origin == "module1" else {2, 3}
    if origin not in {"module1", "module2"} or status not in allowed | {10}:
        raise ValueError(f"平台最新售后状态 {status} 不允许本任务自动退款，需人工核验")
    info = unwrap_order_information(client.get_order_information(order_sn=order.platform_order_sn))
    if str(info.get("order_sn") or "") != order.platform_order_sn:
        raise ValueError("拼多多订单详情身份不一致，禁止自动退款")
    current = normalize_refund({}, detail, info)
    if current.after_sales_type != order.after_sales_type:
        raise ValueError("平台售后类型已变化，禁止按旧任务退款")
    if not current.refund_amount.is_finite() or current.refund_amount <= 0:
        raise ValueError("平台申请退款金额无效，禁止自动退款")
    try:
        unchanged = current.refund_amount == Decimal(str(order.refund_amount))
    except InvalidOperation:
        unchanged = False
    if not unchanged:
        raise ValueError("平台申请退款金额已变化，需重新审核")
    expected = {(item.sku_code, int(item.applied_quantity)) for item in order.items}
    if expected != {(current.item.sku_code, current.item.applied_quantity)}:
        raise ValueError("平台退款型号或数量已变化，需重新验货或审核")
    if origin == "module2" and (
        not current.return_tracking_number
        or current.return_tracking_number != order.return_tracking_number
    ):
        raise ValueError("平台退货运单已变化或缺失，必须重新核验实收")
    if status == 10:
        return True
    if origin == "module1":
        if no_trace_risk_evidence is not None:
            from aftersales_workbench.workflows.no_trace_risk import validate_shipping

            validate_shipping(info, no_trace_risk_evidence, now=now)
        if auto_uncollected_evidence is not None:
            from aftersales_workbench.workflows.auto_uncollected import validate_auto_shipping

            validate_auto_shipping(info, auto_uncollected_evidence, now=now)
        if uncollected_confirmation is not None:
            validate_shipping_snapshot(info, uncollected_confirmation, now=now)
        if current.after_sales_type != AfterSalesType.ONLY_REFUND:
            raise ValueError("模块1只允许已发货仅退款")
        if (
            current.platform_order_amount is None
            or current.platform_order_amount != current.refund_amount
        ):
            raise ValueError("平台最新金额不是买家全额退款，禁止在途自动退款")
        if current.order_shipping_status != ShippingStatus.IN_TRANSIT:
            raise ValueError("平台订单不再是运输中，禁止按旧在途任务自动退款")
        if (
            not current.forward_tracking_number
            or current.forward_tracking_number != order.forward_tracking_number
        ):
            raise ValueError("发货运单已变化，必须重新拦截并核验物流")
    return False


def verify_tmall_refund(client, order, *, origin):
    from aftersales_workbench.integrations.tmall.mapper import (
        normalize_refund,
        unwrap_refund,
        unwrap_trade,
    )
    detail = unwrap_refund(client.get_refund(refund_id=int(order.after_sales_sn)))
    if (
        str(detail.get("refund_id") or "") != order.after_sales_sn
        or str(detail.get("tid") or "") != order.platform_order_sn
    ):
        raise ValueError("天猫最新退款身份与批准订单不一致")
    trade = unwrap_trade(client.get_trade_fullinfo(tid=int(order.platform_order_sn)))
    logistics = client.get_logistics_orders(tid=int(order.platform_order_sn))
    current = normalize_refund({}, detail, trade, logistics)
    if (
        current.after_sales_type != order.after_sales_type
        or current.refund_amount != order.refund_amount
    ):
        raise ValueError("天猫退款类型或金额已变化")
    if not current.refund_amount.is_finite() or current.refund_amount <= 0:
        raise ValueError("天猫退款金额无效")
    if {(i.sku_code, i.applied_quantity) for i in order.items} != {
        (current.item.sku_code, current.item.applied_quantity)
    }:
        raise ValueError("天猫商品或数量已变化，需重新验货")
    if origin == "module1" and (
        current.after_sales_type != AfterSalesType.ONLY_REFUND
        or current.platform_order_amount != current.refund_amount
        or not current.forward_tracking_number
        or current.forward_tracking_number != order.forward_tracking_number
    ):
        raise ValueError("天猫最新申请不满足原批准的全额仅退款/运单条件")
    if origin == "module2" and (
        not current.return_tracking_number
        or current.return_tracking_number != order.return_tracking_number
    ):
        raise ValueError("天猫最新退货运单缺失或已变化")
    return detail
