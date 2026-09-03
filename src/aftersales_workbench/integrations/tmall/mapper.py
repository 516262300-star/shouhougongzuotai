from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from aftersales_workbench.db.models import AfterSalesType, ShippingStatus


class TmallDataMappingError(ValueError):
    """天猫返回数据缺失必要字段或格式不受支持。"""


@dataclass(frozen=True, slots=True)
class NormalizedTmallRefundItem:
    sku_code: str
    applied_quantity: int


@dataclass(frozen=True, slots=True)
class NormalizedTmallRefund:
    after_sales_sn: str
    platform_order_sn: str
    after_sales_type: AfterSalesType
    refund_amount: Decimal
    platform_order_amount: Decimal | None
    platform_goods_amount: Decimal | None
    buyer_reason_raw: str | None
    buyer_memo: str | None
    product_name: str | None
    platform_created_at: datetime | None
    platform_updated_at: datetime | None
    forward_tracking_number: str | None
    return_tracking_number: str | None
    carrier_code: str | None
    platform_after_sales_status_text: str | None
    platform_order_status_text: str | None
    order_shipping_status: ShippingStatus
    item: NormalizedTmallRefundItem


def _nonempty(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _required_text(value: Any, *, field: str) -> str:
    text = _nonempty(value)
    if not text:
        raise TmallDataMappingError(f"缺少 {field}")
    return text


def _money(value: Any, *, field: str, required: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if required:
            raise TmallDataMappingError(f"缺少 {field}")
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise TmallDataMappingError(f"{field} 不是有效金额") from exc
    if amount < 0:
        raise TmallDataMappingError(f"{field} 不能小于 0")
    return amount


def _date(value: Any, *, field: str) -> datetime | None:
    text = _nonempty(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise TmallDataMappingError(f"{field} 不是有效日期") from exc


def _positive_int(value: Any, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TmallDataMappingError(f"{field} 不是整数") from exc
    if result < 1:
        raise TmallDataMappingError(f"{field} 必须大于 0")
    return result


def unwrap_seller(body: dict[str, Any]) -> dict[str, Any]:
    response = body.get("user_seller_get_response")
    seller = response.get("user") if isinstance(response, dict) else None
    if not isinstance(seller, dict):
        raise TmallDataMappingError("缺少 user_seller_get_response.user")
    return seller


def unwrap_refund(body: dict[str, Any]) -> dict[str, Any]:
    response = body.get("refund_get_response")
    refund = response.get("refund") if isinstance(response, dict) else None
    if not isinstance(refund, dict):
        raise TmallDataMappingError("缺少 refund_get_response.refund")
    return refund


def unwrap_trade(body: dict[str, Any]) -> dict[str, Any]:
    response = body.get("trade_fullinfo_get_response")
    trade = response.get("trade") if isinstance(response, dict) else None
    if not isinstance(trade, dict):
        raise TmallDataMappingError("缺少 trade_fullinfo_get_response.trade")
    return trade


def _matching_trade_order(trade: dict[str, Any], oid: str) -> dict[str, Any]:
    orders_node = trade.get("orders")
    orders = orders_node.get("order") if isinstance(orders_node, dict) else None
    if not isinstance(orders, list):
        return {}
    for order in orders:
        if isinstance(order, dict) and str(order.get("oid") or "") == oid:
            return order
    return {}


def _shipping_status(refund: dict[str, Any], trade: dict[str, Any]) -> ShippingStatus:
    status = _nonempty(refund.get("order_status")) or _nonempty(trade.get("status")) or ""
    if status in {"TRADE_FINISHED", "TRADE_SUCCESS"}:
        return ShippingStatus.DELIVERED
    if status in {
        "WAIT_BUYER_CONFIRM_GOODS",
        "SELLER_CONSIGNED_PART",
        "TRADE_BUYER_SIGNED",
    }:
        return ShippingStatus.IN_TRANSIT
    return ShippingStatus.UNSHIPPED


def normalize_forward_logistics(body: dict[str, Any] | None) -> tuple[str | None, str | None]:
    response = (body or {}).get("logistics_orders_get_response")
    shippings_node = response.get("shippings") if isinstance(response, dict) else None
    shippings = shippings_node.get("shipping") if isinstance(shippings_node, dict) else None
    if not isinstance(shippings, list):
        return None, None
    packages: set[tuple[str, str]] = set()
    for shipping in shippings:
        if not isinstance(shipping, dict):
            continue
        status = _nonempty(shipping.get("status")) or ""
        if status in {"CANCELLED", "CLOSED"}:
            continue
        tracking = _nonempty(shipping.get("out_sid"))
        company = _nonempty(shipping.get("company_name")) or ""
        if tracking:
            packages.add((tracking, company))
        mails_node = shipping.get("mails")
        mails = mails_node.get("mail") if isinstance(mails_node, dict) else None
        if isinstance(mails, list):
            for mail in mails:
                if not isinstance(mail, dict):
                    continue
                mail_tracking = _nonempty(mail.get("out_sid"))
                mail_company = _nonempty(mail.get("company_name")) or company
                if mail_tracking:
                    packages.add((mail_tracking, mail_company))
    if len(packages) != 1:
        return None, None
    return next(iter(packages))


def normalize_refund(
    list_record: dict[str, Any],
    detail: dict[str, Any],
    trade: dict[str, Any],
    logistics: dict[str, Any] | None = None,
) -> NormalizedTmallRefund:
    merged = {**list_record, **detail}
    refund_id = _required_text(merged.get("refund_id"), field="refund_id")
    tid = _required_text(merged.get("tid"), field="tid")
    oid = _required_text(merged.get("oid"), field="oid")
    trade_order = _matching_trade_order(trade, oid)
    has_good_return = str(merged.get("has_good_return") or "false").lower() in {
        "1",
        "true",
    }
    refund_amount = _money(merged.get("refund_fee"), field="refund_fee", required=True)
    sku_code = (
        _nonempty(trade_order.get("outer_sku_id"))
        or _nonempty(merged.get("outer_id"))
        or _nonempty(trade_order.get("outer_iid"))
        or oid
    )
    quantity = merged.get("num") or trade_order.get("num") or 1
    forward_tracking, forward_carrier = normalize_forward_logistics(logistics)
    return NormalizedTmallRefund(
        after_sales_sn=refund_id,
        platform_order_sn=tid,
        after_sales_type=(
            AfterSalesType.RETURN_AND_REFUND if has_good_return else AfterSalesType.ONLY_REFUND
        ),
        refund_amount=refund_amount,  # type: ignore[arg-type]
        platform_order_amount=(
            _money(merged.get("payment"), field="payment")
            or _money(trade.get("payment"), field="trade.payment")
        ),
        platform_goods_amount=(
            _money(merged.get("total_fee"), field="total_fee")
            or _money(trade.get("total_fee"), field="trade.total_fee")
        ),
        buyer_reason_raw=_nonempty(merged.get("reason")),
        buyer_memo=_nonempty(merged.get("desc")),
        product_name=_nonempty(merged.get("title")) or _nonempty(trade_order.get("title")),
        platform_created_at=_date(merged.get("created"), field="created"),
        platform_updated_at=_date(merged.get("modified"), field="modified"),
        forward_tracking_number=forward_tracking,
        return_tracking_number=_nonempty(merged.get("sid")),
        carrier_code=forward_carrier,
        platform_after_sales_status_text=_nonempty(merged.get("status")),
        platform_order_status_text=(
            _nonempty(merged.get("order_status")) or _nonempty(trade.get("status"))
        ),
        order_shipping_status=_shipping_status(merged, trade),
        item=NormalizedTmallRefundItem(
            sku_code=sku_code,
            applied_quantity=_positive_int(quantity, field="num"),
        ),
    )
