"""天猫发货事实与退款结果分离；关闭或缺失不能成为未发货证据。"""

from datetime import datetime
from typing import Any

from aftersales_workbench.db.models import ShippingStatus

UNSHIPPED_STATUSES = frozenset({"WAIT_SELLER_SEND_GOODS", "WAIT_BUYER_PAY", "TRADE_NO_CREATE_PAY"})
DELIVERED_STATUSES = frozenset({"TRADE_FINISHED", "TRADE_SUCCESS", "TRADE_BUYER_SIGNED"})
SHIPPED_STATUSES = frozenset({"WAIT_BUYER_CONFIRM_GOODS", "SELLER_CONSIGNED_PART"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def shipping_rows(body: dict[str, Any] | None) -> list[dict[str, Any]]:
    response = (body or {}).get("logistics_orders_get_response")
    node = response.get("shippings") if isinstance(response, dict) else None
    rows = node.get("shipping") if isinstance(node, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def classify_shipping(refund: dict, trade: dict, logistics: dict | None = None) -> ShippingStatus:
    node = trade.get("orders")
    rows = node.get("order") if isinstance(node, dict) else None
    orders = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    matched = [r for r in orders if _text(r.get("oid")) == _text(refund.get("oid"))]
    statuses = {
        _text(refund.get("order_status")), _text(trade.get("status")),
        *(_text(r.get("status")) for r in matched),
    } - {""}
    if statuses & DELIVERED_STATUSES:
        return ShippingStatus.DELIVERED
    # 模块3会取消整笔ERP订单；同父单其他子单已发货，也禁止整单按未发货处理。
    all_statuses = statuses | {_text(r.get("status")) for r in orders}
    if all_statuses & (SHIPPED_STATUSES | DELIVERED_STATUSES):
        return ShippingStatus.IN_TRANSIT
    times = [_text(r.get("consign_time")) for r in [trade, *orders]]
    for value in times:
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                return ShippingStatus.IN_TRANSIT
            except ValueError:
                pass
    shippings = shipping_rows(logistics)
    if any(_text(r.get("seller_confirm")).lower() == "yes" for r in shippings):
        return ShippingStatus.IN_TRANSIT
    # 快递订单关闭或不存在均不能证明从未交运；运单号单独存在也不等于已发货。
    if any(times) or shippings:
        return ShippingStatus.UNKNOWN
    if statuses and statuses <= UNSHIPPED_STATUSES:
        return ShippingStatus.UNSHIPPED
    return ShippingStatus.UNKNOWN


def preserve_shipping(previous: ShippingStatus | str, incoming: ShippingStatus) -> ShippingStatus:
    """已记录的发货/签收事实不能被退款关闭或缺字段降级。"""
    previous = ShippingStatus(previous)
    if previous is ShippingStatus.DELIVERED:
        return previous
    if previous is ShippingStatus.IN_TRANSIT and incoming is not ShippingStatus.DELIVERED:
        return previous
    if previous is ShippingStatus.PACKED_NOT_SHIPPED and incoming is ShippingStatus.UNSHIPPED:
        return previous
    return incoming
