from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AfterSalesType, ShippingStatus
from aftersales_workbench.integrations.marketplace.http import RetryingJsonClient
from aftersales_workbench.integrations.marketplace.mapping import (
    list_of_mappings,
    money,
    nonempty,
    parse_datetime,
    positive_int,
    required_text,
)
from aftersales_workbench.integrations.marketplace.models import (
    ConfiguredMarketplaceShop,
    MarketplaceApiError,
    NormalizedMarketplaceItem,
    NormalizedMarketplaceRefund,
)

REFUND_LIST_API = "alibaba.trade.refund.queryOrderRefundList"
REFUND_DETAIL_API = "alibaba.trade.refund.OpQueryOrderRefund"
ORDER_DETAIL_API = "alibaba.trade.ec.getOrder.sellerView"


def generate_1688_sign(
    api_path: str,
    parameters: dict[str, Any],
    app_secret: str,
) -> str:
    parts = sorted(
        f"{key}{value}"
        for key, value in parameters.items()
        if value is not None and not isinstance(value, (dict, list))
    )
    source = api_path + "".join(parts)
    return hmac.new(
        app_secret.encode("utf-8"),
        source.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest().upper()


class Alibaba1688ReadClient(RetryingJsonClient):
    def __init__(self, config: ConfiguredMarketplaceShop, settings: Settings, **kwargs: Any):
        super().__init__(
            timeout_seconds=settings.marketplace_timeout_seconds,
            read_max_attempts=settings.marketplace_read_max_attempts,
            **kwargs,
        )
        self.config = config
        self.api_url = settings.alibaba_1688_api_url.rstrip("/")

    def identity(self) -> tuple[str, str]:
        return self.config.platform_shop_id, self.config.shop_name

    def execute_read(self, namespace: str, api: str, **parameters: Any) -> dict[str, Any]:
        app_key = self.config.app_key.get_secret_value().strip()
        api_path = f"param2/1/{namespace}/{api}/{app_key}"
        signature = generate_1688_sign(
            api_path,
            parameters,
            self.config.app_secret.get_secret_value().strip(),
        )
        query = {
            key: value
            for key, value in parameters.items()
            if value is not None and not isinstance(value, (dict, list))
        }
        query["_aop_signature"] = signature
        url = f"{self.api_url}/{api_path}?{urlencode(query, quote_via=quote)}"
        body = self.request_json("GET", url)
        error_code = body.get("errorCode") or body.get("error_code")
        if error_code:
            raise MarketplaceApiError(
                "1688 API error: "
                f"code={error_code}, "
                f"message={body.get('errorMessage') or body.get('error_message')}"
            )
        return body

    def get_refunds(self, *, start_at: int, end_at: int, page: int) -> dict[str, Any]:
        def format_time(value: int) -> str:
            return datetime.fromtimestamp(value).strftime("%Y%m%d%H%M%S000+0800")

        return self.execute_read(
            "com.alibaba.trade",
            REFUND_LIST_API,
            modifyStartTime=format_time(start_at),
            modifyEndTime=format_time(end_at),
            currentPageNum=page,
            pageSize=20,
            dipsuteType="0",
        )

    def get_refund_detail(self, refund_id: str) -> dict[str, Any]:
        return self.execute_read(
            "com.alibaba.trade",
            REFUND_DETAIL_API,
            refundId=refund_id,
        )

    def get_order_detail(self, order_id: str) -> dict[str, Any]:
        return self.execute_read(
            "com.alibaba.trade",
            ORDER_DETAIL_API,
            webSite="1688",
            orderId=order_id,
        )

    def fetch_window(
        self,
        *,
        start_modified_at: int,
        end_modified_at: int,
        page_size: int,
    ):
        del page_size
        page = 1
        while True:
            body = self.get_refunds(
                start_at=start_modified_at,
                end_at=end_modified_at,
                page=page,
            )
            result = body.get("result")
            if not isinstance(result, dict):
                raise ValueError("1688 退款列表缺少 result")
            records = list_of_mappings(result.get("opOrderRefundModels"))
            for record in records:
                refund_id = required_text(record.get("refundId"), field="refundId")
                detail_body = self.get_refund_detail(refund_id)
                detail_result = detail_body.get("result")
                detail = (
                    detail_result.get("opOrderRefundModelDetail")
                    if isinstance(detail_result, dict)
                    else None
                )
                if not isinstance(detail, dict):
                    raise ValueError(f"1688 售后 {refund_id} 缺少退款详情")
                order_id = required_text(detail.get("orderId"), field="orderId")
                order_body = self.get_order_detail(order_id)
                order = order_body.get("result")
                if not isinstance(order, dict):
                    order = {}
                yield normalize_1688_refund(detail, order)
            if len(records) < 20:
                break
            page += 1
            if page > 1000:
                raise ValueError("1688 退款分页超过 1000 页")


def _shipping_status(order: dict[str, Any]) -> ShippingStatus:
    base = order.get("baseInfo") if isinstance(order.get("baseInfo"), dict) else {}
    status = str(base.get("status") or base.get("tradeStatus") or "").lower()
    if status in {"success", "trade_success", "trade_finished"}:
        return ShippingStatus.DELIVERED
    if status in {
        "waitbuyerreceive",
        "waitbuyerconfirm",
        "sellerconsigned",
        "waitlogisticstakein",
    }:
        return ShippingStatus.IN_TRANSIT
    return ShippingStatus.UNSHIPPED


def _forward_logistics(order: dict[str, Any]) -> tuple[str | None, str | None]:
    native = order.get("nativeLogistics")
    if isinstance(native, list):
        native = native[0] if native else {}
    if not isinstance(native, dict):
        return None, None
    return (
        nonempty(native.get("logisticsBillNo") or native.get("logisticsId")),
        nonempty(native.get("logisticsCompanyName") or native.get("logisticsCompanyId")),
    )


def normalize_1688_refund(
    detail: dict[str, Any],
    order: dict[str, Any],
) -> NormalizedMarketplaceRefund:
    refund_id = required_text(detail.get("refundId"), field="refundId")
    order_id = required_text(detail.get("orderId"), field="orderId")
    entry_counts = detail.get("orderEntryCountMap")
    if not isinstance(entry_counts, dict) or not entry_counts:
        entry_counts = {order_id: 1}
    product_items = {
        str(item.get("subItemID") or item.get("subItemIDString") or ""): item
        for item in list_of_mappings(order.get("productItems"))
    }
    items: list[NormalizedMarketplaceItem] = []
    for entry_id, quantity in entry_counts.items():
        product = product_items.get(str(entry_id), {})
        sku = (
            nonempty(product.get("cargoNumber"))
            or nonempty(product.get("skuID"))
            or str(entry_id)
        )
        items.append(
            NormalizedMarketplaceItem(
                sku_code=sku,
                applied_quantity=positive_int(quantity, field="orderEntryCountMap"),
                product_name=nonempty(
                    product.get("name") or product.get("productName")
                ),
            )
        )
    apply_payment = money(detail.get("applyPayment"), field="applyPayment", divisor=100)
    apply_carriage = money(
        detail.get("applyCarriage"), field="applyCarriage", divisor=100
    )
    refund_amount = (apply_payment or Decimal("0")) + (
        apply_carriage or Decimal("0")
    )
    if refund_amount <= 0:
        raise ValueError(f"1688 售后 {refund_id} 缺少有效退款金额")
    base = order.get("baseInfo") if isinstance(order.get("baseInfo"), dict) else {}
    goods_amount = sum(
        (money(item.get("itemAmount"), field="itemAmount") or Decimal("0"))
        for item in product_items.values()
    ) or None
    forward_tracking, forward_carrier = _forward_logistics(order)
    refund_goods = str(
        detail.get("refundGoods") or detail.get("isRefundGoods") or "false"
    ).lower() in {"1", "true"}
    return NormalizedMarketplaceRefund(
        after_sales_sn=refund_id,
        platform_order_sn=order_id,
        after_sales_type=(
            AfterSalesType.RETURN_AND_REFUND
            if refund_goods
            else AfterSalesType.ONLY_REFUND
        ),
        refund_amount=refund_amount,
        platform_order_amount=money(
            base.get("totalAmount") or base.get("sumProductPayment"),
            field="baseInfo.totalAmount",
        ),
        platform_goods_amount=goods_amount,
        buyer_reason_raw=nonempty(detail.get("applyReason")),
        buyer_memo=nonempty(detail.get("description") or detail.get("applyDesc")),
        product_name=next(
            (item.product_name for item in items if item.product_name), None
        ),
        platform_created_at=parse_datetime(detail.get("gmtApply")),
        platform_updated_at=parse_datetime(
            detail.get("gmtModified") or detail.get("gmtCompleted")
        ),
        forward_tracking_number=forward_tracking,
        return_tracking_number=nonempty(detail.get("freightBill")),
        carrier_code=(
            nonempty(detail.get("buyerLogisticsName")) or forward_carrier
        ),
        order_shipping_status=_shipping_status(order),
        platform_after_sales_status_text=nonempty(detail.get("status")),
        platform_order_status_text=nonempty(
            base.get("status") or base.get("tradeStatus")
        ),
        items=tuple(items),
    )
