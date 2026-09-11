"""失败退款的业务解释；只观察/转人工，不授权或重试资金写入。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.pdd.mapper import unwrap_order_information

CASE_KEY = "pdd_refund_case"
SUSPECTED = "TYPE_SELECTION_SUSPECTED"
CONFIRMED = "TYPE_SELECTION_CONFIRMED"
RELATED_SUCCESS = "ORDER_REFUNDED_ELSEWHERE"
CORRECTED = "TYPE_CORRECTED_RECHECK"
CHANGED = "TYPE_CHANGED"
CASE_MESSAGES = {
    SUSPECTED: "已发送快递拦截，但申请变成退货退款，请确认客户是否选错类型",
    CONFIRMED: "已确认客户误选退货退款，请协助更正平台申请，再核验退款条件",
    RELATED_SUCCESS: "同订单已通过其他售后退款，旧任务停止处理；退货平账仍需核验",
    CORRECTED: "平台申请已改回仅退款，请重新核验退款条件，勿直接重试旧任务",
    CHANGED: "平台售后类型已变化，请核对客户真实需求及当前处理方式",
}
CASE_LABELS = {
    SUSPECTED: "疑似选错类型·待确认",
    CONFIRMED: "已确认选错·待更正申请",
    RELATED_SUCCESS: "同订单已退款·其他售后",
    CORRECTED: "申请已更正·待重新核验",
    CHANGED: "售后类型变化·待核验",
}


def _identity(detail, order, refund_id):
    if (
        str(detail.get("id") or "") != refund_id
        or detail.get("order_sn") != order.platform_order_sn
    ):
        raise ValueError("售后身份不一致，不能关联新旧申请")


def _money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("退款关联金额无效")
    return amount


def _item(detail):
    sku = str(detail.get("out_sku_sn") or "").strip()
    qty = int(detail.get("goods_number") or 0)
    if not sku or qty <= 0:
        raise ValueError("退款关联缺少有效型号数量")
    return sku, qty


def observe_case(session, client, order, task, detail):
    """平台详情必须在当前调用内读取；不把留言或空退货单号当作意图确认。"""
    if (task.payload or {}).get("origin") != "module1":
        return None
    _identity(detail, order, order.after_sales_sn)
    current_type = int(detail.get("after_sales_type") or 0)
    status = int(detail.get("after_sales_status") or 0)
    if current_type not in {1, 2}:
        raise ValueError("当前售后不是可关联的仅退款/退货退款")
    if status == 10:
        return None  # 同张售后已成功仍走原只读资金核验，不复制到账金额。
    prior = (task.payload or {}).get(CASE_KEY) or {}
    others = list(
        session.scalars(
            select(AfterSalesOrder).where(
                AfterSalesOrder.shop_id == order.shop_id,
                AfterSalesOrder.platform_order_sn == order.platform_order_sn,
                AfterSalesOrder.after_sales_sn != order.after_sales_sn,
                AfterSalesOrder.platform_after_sales_status == 10,
                AfterSalesOrder.after_sales_type.in_(
                    (AfterSalesType.ONLY_REFUND, AfterSalesType.RETURN_AND_REFUND)
                ),
            )
        )
    )
    if current_type == 1 and not others and not prior:
        return None
    info = unwrap_order_information(client.get_order_information(order_sn=order.platform_order_sn))
    if info.get("order_sn") != order.platform_order_sn:
        raise ValueError("订单详情身份不一致，不能关联新旧售后")
    amount = _money(detail.get("refund_amount")) / 100
    paid = _money(info.get("pay_amount"))
    item = _item(detail)
    expected = {(i.sku_code, int(i.applied_quantity)) for i in order.items}
    same_goods = expected == {item}
    full = same_goods and amount == paid == order.refund_amount == order.platform_order_amount
    tracking = str(info.get("tracking_number") or "").strip()
    same_package = bool(tracking and tracking == order.forward_tracking_number)
    base = dict(
        checked_at=datetime.now(UTC).isoformat(),
        platform_type=current_type,
        platform_status=status,
        platform_order_sn=order.platform_order_sn,
        after_sales_sn=order.after_sales_sn,
        refund_amount=str(amount),
        sku=item[0],
        quantity=item[1],
        tracking_number=tracking,
        previous_type=(prior.get("previous_type") or str(order.after_sales_type)),
        platform_updated_time=str(detail.get("updated_time") or ""),
    )
    matched = []
    if full and same_package:
        for sibling in others:
            observed = client.get_refund_information(
                order_sn=order.platform_order_sn,
                after_sales_id=int(sibling.after_sales_sn),
            )
            _identity(observed, order, sibling.after_sales_sn)
            if (
                int(observed.get("after_sales_type") or 0) in {1, 2}
                and int(observed.get("after_sales_status") or 0) == 10
                and _money(observed.get("refund_amount")) / 100 == paid
                and _item(observed) == item
                and sibling.forward_tracking_number == tracking
            ):
                matched.append(sibling.after_sales_sn)
    if len(matched) == 1:
        return {**base, "code": RELATED_SUCCESS, "related_after_sales_sn": matched[0]}
    if current_type == 1:
        return (
            {**base, "code": CORRECTED} if prior and prior.get("code") != RELATED_SUCCESS else None
        )
    notices = session.scalars(
        select(AftersalesActionTask).where(
            AftersalesActionTask.after_sales_sn == order.after_sales_sn,
            AftersalesActionTask.action_type == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
            AftersalesActionTask.action_status == AutomationTaskStatus.SUCCEEDED,
        )
    )
    sent = any((n.payload or {}).get("tracking_number") == tracking for n in notices)
    code = (
        SUSPECTED
        if full
        and same_package
        and sent
        and not str(detail.get("express_no") or "").strip()
        and len(matched) == 0
        else CHANGED
    )
    confirmation = (task.payload or {}).get("type_intent_confirmation") or {}
    # 人工确认必须绑定当前售后/金额/SKU/包裹，变化后重新核验，不能沿用确认。
    if (
        code == SUSPECTED
        and all(
            confirmation.get(k) == base[k]
            for k in (
                "after_sales_sn",
                "platform_order_sn",
                "refund_amount",
                "sku",
                "quantity",
                "tracking_number",
                "platform_updated_time",
            )
        )
        and confirmation.get("source") in {"user", "salesperson", "customer"}
        and confirmation.get("operator")
        and confirmation.get("note")
    ):
        code = CONFIRMED
    return {**base, "code": code}


def apply_case(task, order, case):
    """不把旧任务改成退款成功，不修改实际到账金额，也不重新入队。"""
    original = (task.payload or {}).get("original_execution_error") or task.last_error
    payload = dict(task.payload or {})
    if payload.get(CASE_KEY) and payload[CASE_KEY].get("code") != case["code"]:
        payload["previous_refund_case"] = payload[CASE_KEY]
    task.payload = {**payload, "original_execution_error": original, CASE_KEY: case}
    code = case["code"]
    order.after_sales_type = (
        AfterSalesType.RETURN_AND_REFUND
        if case["platform_type"] == 2
        else AfterSalesType.ONLY_REFUND
    )
    order.platform_after_sales_status = case["platform_status"]
    order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
    order.exception_type = CASE_MESSAGES[code]
    task.last_error = CASE_MESSAGES[code]
    # 已有/已发待办保留发布证据，不重置任务或重复发通知。


def case_for_display(task):
    case = ((task.payload or {}).get(CASE_KEY) or {}) if task else {}
    return case if case.get("code") in CASE_LABELS else None
