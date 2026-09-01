from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from aftersales_workbench.db.models import AfterSalesType, ShippingStatus


class PddDataMappingError(ValueError):
    """拼多多返回数据缺失必要字段或字段值未受支持。"""


@dataclass(frozen=True, slots=True)
class NormalizedRefundItem:
    sku_code: str
    applied_quantity: int


@dataclass(frozen=True, slots=True)
class NormalizedRefund:
    after_sales_sn: str
    platform_order_sn: str
    after_sales_type: AfterSalesType
    refund_amount: Decimal
    platform_order_amount: Decimal | None
    platform_goods_amount: Decimal | None
    platform_discount_amount: Decimal | None
    seller_discount_amount: Decimal | None
    merchant_receivable_amount: Decimal | None
    buyer_reason_raw: str | None
    buyer_memo: str | None
    forward_tracking_number: str | None
    carrier_code: str | None
    return_tracking_number: str | None
    platform_after_sales_status: int | None
    platform_order_refund_status: int | None
    is_speed_refund: bool
    order_shipping_status: ShippingStatus
    item: NormalizedRefundItem


def _nonempty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: Any, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PddDataMappingError(f"{field} 不是整数") from exc
    if result < 1:
        raise PddDataMappingError(f"{field} 必须大于 0")
    return result


def _optional_int(value: Any, *, field: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PddDataMappingError(f"{field} 不是整数") from exc


def _refund_amount(detail: dict[str, Any], list_record: dict[str, Any]) -> Decimal:
    detail_value = detail.get("refund_amount")
    if detail_value is not None:
        try:
            return (Decimal(str(detail_value)) / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except InvalidOperation as exc:
            raise PddDataMappingError("refund_amount 不是有效金额") from exc
    try:
        return Decimal(str(list_record["refund_amount"])).quantize(Decimal("0.01"))
    except (KeyError, InvalidOperation) as exc:
        raise PddDataMappingError("缺少有效 refund_amount") from exc


def platform_order_amount(
    detail: dict[str, Any],
    order: dict[str, Any],
) -> Decimal | None:
    """返回优惠后的应退基准金额；详情按分，订单接口按元。"""
    detail_value = detail.get("order_amount")
    if detail_value is not None and str(detail_value).strip() != "":
        try:
            amount = Decimal(str(detail_value)) / Decimal("100")
        except InvalidOperation as exc:
            raise PddDataMappingError("order_amount 不是有效金额") from exc
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    order_value = order.get("pay_amount")
    if order_value is None or str(order_value).strip() == "":
        return None
    try:
        return Decimal(str(order_value)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except InvalidOperation as exc:
        raise PddDataMappingError("pay_amount 不是有效金额") from exc


def _optional_order_amount(order: dict[str, Any], field: str) -> Decimal | None:
    value = order.get(field)
    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise PddDataMappingError(f"{field} 不是有效金额") from exc
    if amount < 0:
        raise PddDataMappingError(f"{field} 不能小于 0")
    return amount


@dataclass(frozen=True, slots=True)
class PddOrderAmountBreakdown:
    buyer_paid_amount: Decimal | None
    goods_amount: Decimal | None
    platform_discount_amount: Decimal | None
    seller_discount_amount: Decimal | None
    merchant_receivable_amount: Decimal | None


def pdd_order_amount_breakdown(
    detail: dict[str, Any],
    order: dict[str, Any],
) -> PddOrderAmountBreakdown:
    """拆分买家实付、平台补贴和商家应收，订单接口金额单位均为元。"""
    buyer_paid = platform_order_amount(detail, order)
    platform_discount = _optional_order_amount(order, "platform_discount")
    seller_discount = _optional_order_amount(order, "seller_discount")
    goods_amount = _optional_order_amount(order, "goods_amount")
    merchant_receivable = (
        (buyer_paid + platform_discount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if buyer_paid is not None and platform_discount is not None
        else None
    )
    return PddOrderAmountBreakdown(
        buyer_paid_amount=buyer_paid,
        goods_amount=goods_amount,
        platform_discount_amount=platform_discount,
        seller_discount_amount=seller_discount,
        merchant_receivable_amount=merchant_receivable,
    )


def _after_sales_type(list_value: Any, detail_value: Any) -> AfterSalesType:
    list_mapping = {
        2: AfterSalesType.ONLY_REFUND,
        3: AfterSalesType.RETURN_AND_REFUND,
        4: AfterSalesType.EXCHANGE,
    }
    detail_mapping = {
        1: AfterSalesType.ONLY_REFUND,
        2: AfterSalesType.RETURN_AND_REFUND,
        3: AfterSalesType.EXCHANGE,
    }
    value = list_value if list_value is not None else detail_value
    mapping = list_mapping if list_value is not None else detail_mapping
    try:
        return mapping[int(value)]
    except (KeyError, TypeError, ValueError) as exc:
        source = "增量列表" if list_value is not None else "售后详情"
        raise PddDataMappingError(f"不支持的 {source} after_sales_type: {value}") from exc


def _shipping_status(order: dict[str, Any]) -> ShippingStatus:
    order_status = order.get("order_status")
    if order_status == 1:
        return ShippingStatus.UNSHIPPED
    if order_status == 2:
        return ShippingStatus.IN_TRANSIT
    if order_status == 3:
        return ShippingStatus.DELIVERED
    if _nonempty(order.get("receive_time")):
        return ShippingStatus.DELIVERED
    if _nonempty(order.get("tracking_number")) or _nonempty(order.get("shipping_time")):
        return ShippingStatus.IN_TRANSIT
    return ShippingStatus.UNSHIPPED


def unwrap_order_information(body: dict[str, Any]) -> dict[str, Any]:
    response = body.get("order_info_get_response")
    if not isinstance(response, dict):
        raise PddDataMappingError("缺少 order_info_get_response")
    order = response.get("order_info")
    if not isinstance(order, dict):
        raise PddDataMappingError("缺少 order_info")
    return order


def normalize_refund(
    list_record: dict[str, Any],
    detail: dict[str, Any],
    order: dict[str, Any],
) -> NormalizedRefund:
    after_sales_sn = _nonempty(detail.get("id") or list_record.get("id"))
    order_sn = _nonempty(detail.get("order_sn") or list_record.get("order_sn"))
    if not after_sales_sn:
        raise PddDataMappingError("缺少售后单 id")
    if not order_sn:
        raise PddDataMappingError("缺少 order_sn")

    sku_code = _nonempty(
        detail.get("out_sku_sn")
        or list_record.get("outer_id")
        or detail.get("out_goods_sn")
        or detail.get("sku_id")
        or list_record.get("sku_id")
    )
    if not sku_code:
        raise PddDataMappingError("缺少 SKU 标识")

    amounts = pdd_order_amount_breakdown(detail, order)
    return NormalizedRefund(
        after_sales_sn=after_sales_sn,
        platform_order_sn=order_sn,
        after_sales_type=_after_sales_type(
            list_record.get("after_sales_type"), detail.get("after_sales_type")
        ),
        refund_amount=_refund_amount(detail, list_record),
        platform_order_amount=amounts.buyer_paid_amount,
        platform_goods_amount=amounts.goods_amount,
        platform_discount_amount=amounts.platform_discount_amount,
        seller_discount_amount=amounts.seller_discount_amount,
        merchant_receivable_amount=amounts.merchant_receivable_amount,
        buyer_reason_raw=_nonempty(
            detail.get("after_sales_reason") or list_record.get("after_sale_reason")
        ),
        buyer_memo=_nonempty(detail.get("remark") or order.get("buyer_memo")),
        forward_tracking_number=_nonempty(order.get("tracking_number")),
        carrier_code=_nonempty(order.get("logistics_id")),
        return_tracking_number=_nonempty(
            detail.get("express_no") or list_record.get("tracking_number")
        ),
        platform_after_sales_status=_optional_int(
            list_record.get("after_sales_status", detail.get("after_sales_status")),
            field="after_sales_status",
        ),
        platform_order_refund_status=_optional_int(
            order.get("refund_status"), field="refund_status"
        ),
        is_speed_refund=str(
            list_record.get("speed_refund_flag", detail.get("speed_refund_flag", 0))
        ).strip()
        == "1",
        order_shipping_status=_shipping_status(order),
        item=NormalizedRefundItem(
            sku_code=sku_code,
            applied_quantity=_positive_int(
                detail.get("goods_number", list_record.get("goods_number")),
                field="goods_number",
            ),
        ),
    )
