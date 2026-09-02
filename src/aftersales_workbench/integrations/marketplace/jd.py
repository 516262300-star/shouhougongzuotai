from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

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

JD_REFUND_LIST = "jingdong.pop.afs.soa.refundapply.queryPageList"
JD_AFTERSALE_LIST = "jingdong.asc.serviceAndRefund.view"
JD_ORDER_GET = "jingdong.pop.order.get"


def generate_jd_sign(parameters: dict[str, Any], app_secret: str) -> str:
    source = app_secret + "".join(
        f"{key}{value}"
        for key, value in sorted(parameters.items())
        if key != "sign" and value is not None
    ) + app_secret
    return hashlib.md5(source.encode("utf-8"), usedforsecurity=False).hexdigest().upper()


class JdReadClient(RetryingJsonClient):
    def __init__(self, config: ConfiguredMarketplaceShop, settings: Settings, **kwargs: Any):
        if config.access_token is None:
            raise ValueError("京东店铺缺少 access_token")
        super().__init__(
            timeout_seconds=settings.marketplace_timeout_seconds,
            read_max_attempts=settings.marketplace_read_max_attempts,
            **kwargs,
        )
        self.config = config
        self.api_url = settings.jd_api_url
        self.request_method = settings.jd_request_method

    def identity(self) -> tuple[str, str]:
        return self.config.platform_shop_id, self.config.shop_name

    def execute_read(self, method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, str] = {
            "method": method,
            "app_key": self.config.app_key.get_secret_value().strip(),
            "access_token": self.config.access_token.get_secret_value().strip(),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "v": "2.0",
            "format": "json",
            "360buy_param_json": json.dumps(
                parameters,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        payload["sign"] = generate_jd_sign(
            payload,
            self.config.app_secret.get_secret_value().strip(),
        )
        request_kwargs = (
            {"params": payload}
            if self.request_method == "GET"
            else {"data": payload}
        )
        body = self.request_json(self.request_method, self.api_url, **request_kwargs)
        error = body.get("error_response")
        if isinstance(error, dict):
            raise MarketplaceApiError(
                "JD API error: "
                f"code={error.get('code')}, message={error.get('zh_desc') or error.get('en_desc')}"
            )
        return body

    def get_order(self, order_id: str) -> dict[str, Any]:
        body = self.execute_read(
            JD_ORDER_GET,
            {
                "order_id": order_id,
                "optional_fields": (
                    "modified,waybill,logisticsId,orderState,itemInfoList,venderId,"
                    "outerSkuId,outerId,orderPayment,orderSellerPrice,paymentConfirmTime"
                ),
            },
        )
        response = body.get("jingdong_pop_order_get_responce") or body.get(
            "jingdong_pop_order_get_response"
        )
        detail = response.get("orderDetailInfo") if isinstance(response, dict) else None
        order = detail.get("orderInfo") if isinstance(detail, dict) else None
        return order if isinstance(order, dict) else {}

    def fetch_window(
        self,
        *,
        start_modified_at: int,
        end_modified_at: int,
        page_size: int,
    ):
        limit = min(page_size, 50)
        order_cache: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        start_text = datetime.fromtimestamp(start_modified_at).strftime("%Y-%m-%d %H:%M:%S")
        end_text = datetime.fromtimestamp(end_modified_at).strftime("%Y-%m-%d %H:%M:%S")

        page = 1
        while True:
            body = self.execute_read(
                JD_REFUND_LIST,
                {
                    "applyTimeStart": start_text,
                    "applyTimeEnd": end_text,
                    "pageIndex": page,
                    "pageSize": limit,
                },
            )
            response = body.get(
                "jingdong_pop_afs_soa_refundapply_queryPageList_responce"
            ) or body.get("jingdong_pop_afs_soa_refundapply_queryPageList_response")
            query_result = response.get("queryResult") if isinstance(response, dict) else None
            records = list_of_mappings(
                query_result.get("result") if isinstance(query_result, dict) else None
            )
            for record in records:
                refund_id = required_text(
                    record.get("id") or record.get("raId"), field="refund id"
                )
                if refund_id in seen:
                    continue
                order_id = required_text(record.get("orderId"), field="orderId")
                if order_id not in order_cache:
                    order_cache[order_id] = self.get_order(order_id)
                order = order_cache[order_id]
                yield normalize_jd_refund_apply(record, order)
                seen.add(refund_id)
            if len(records) < limit:
                break
            page += 1
            if page > 1000:
                raise ValueError("京东退款申请分页超过 1000 页")

        page = 1
        while True:
            body = self.execute_read(
                JD_AFTERSALE_LIST,
                {
                    "applyTimeBegin": start_text,
                    "applyTimeEnd": end_text,
                    "pageIndex": page,
                    "pageSize": limit,
                },
            )
            response = body.get("jingdong_asc_serviceAndRefund_view_responce") or body.get(
                "jingdong_asc_serviceAndRefund_view_response"
            )
            page_result = response.get("pageResult") if isinstance(response, dict) else None
            records = list_of_mappings(
                page_result.get("data") if isinstance(page_result, dict) else None
            )
            for record in records:
                bill = record.get("sameOrderServiceBill")
                if not isinstance(bill, dict):
                    continue
                service_id = required_text(bill.get("serviceId"), field="serviceId")
                if service_id in seen:
                    continue
                order_id = required_text(bill.get("orderId"), field="orderId")
                if order_id not in order_cache:
                    order_cache[order_id] = self.get_order(order_id)
                order = order_cache[order_id]
                yield normalize_jd_aftersale(record, bill, order)
                seen.add(service_id)
            if len(records) < limit:
                break
            page += 1
            if page > 1000:
                raise ValueError("京东退货售后分页超过 1000 页")


def _shipping_status(order: dict[str, Any]) -> ShippingStatus:
    status = str(order.get("orderState") or "").upper()
    if status in {"FINISHED_L", "TRADE_FINISHED", "COMPLETED", "FINISHED"}:
        return ShippingStatus.DELIVERED
    if any(word in status for word in ("RECEIVE", "DELIVERY", "SHIPPED", "OUTBOUND")):
        return ShippingStatus.IN_TRANSIT
    return ShippingStatus.UNSHIPPED


def _order_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    return list_of_mappings(order.get("itemInfoList"))


def _item_from_order(item: dict[str, Any], quantity: Any = None) -> NormalizedMarketplaceItem:
    return NormalizedMarketplaceItem(
        sku_code=(
            nonempty(item.get("outerSkuId"))
            or nonempty(item.get("skuId"))
            or nonempty(item.get("wareId"))
            or "JD-UNKNOWN-SKU"
        ),
        applied_quantity=positive_int(
            quantity if quantity is not None else item.get("itemTotal"),
            field="serviceCount",
        ),
        product_name=nonempty(item.get("wareName") or item.get("productName")),
    )


def normalize_jd_refund_apply(
    record: dict[str, Any],
    order: dict[str, Any],
) -> NormalizedMarketplaceRefund:
    refund_id = required_text(record.get("id") or record.get("raId"), field="refund id")
    order_id = required_text(record.get("orderId"), field="orderId")
    amount = money(
        record.get("applyRefundSum") or record.get("apply_refund_sum"),
        field="applyRefundSum",
        divisor=100,
    )
    if amount is None or amount <= 0:
        raise ValueError(f"京东退款 {refund_id} 缺少有效退款金额")
    order_items = _order_items(order)
    if len(order_items) == 1:
        items = (_item_from_order(order_items[0]),)
    else:
        items = (NormalizedMarketplaceItem(refund_id, 1),)
    return NormalizedMarketplaceRefund(
        after_sales_sn=refund_id,
        platform_order_sn=order_id,
        after_sales_type=AfterSalesType.ONLY_REFUND,
        refund_amount=amount,
        platform_order_amount=money(order.get("orderPayment"), field="orderPayment"),
        platform_goods_amount=money(
            order.get("orderSellerPrice"), field="orderSellerPrice"
        ),
        buyer_reason_raw=nonempty(record.get("reason")),
        buyer_memo=nonempty(record.get("remark") or record.get("applyReason")),
        product_name=items[0].product_name,
        platform_created_at=parse_datetime(
            record.get("applyTime") or record.get("created")
        ),
        platform_updated_at=parse_datetime(
            record.get("modified") or order.get("modified")
        ),
        forward_tracking_number=nonempty(order.get("waybill")),
        return_tracking_number=None,
        carrier_code=nonempty(order.get("logisticsId")),
        order_shipping_status=_shipping_status(order),
        platform_after_sales_status_text=nonempty(record.get("status")),
        platform_order_status_text=nonempty(order.get("orderState")),
        items=items,
    )


def normalize_jd_aftersale(
    record: dict[str, Any],
    bill: dict[str, Any],
    order: dict[str, Any],
) -> NormalizedMarketplaceRefund:
    service_id = required_text(bill.get("serviceId"), field="serviceId")
    order_id = required_text(bill.get("orderId"), field="orderId")
    amount = money(
        record.get("refoundAmount") or record.get("refundAmount"),
        field="refoundAmount",
    )
    if amount is None or amount <= 0:
        raise ValueError(f"京东售后 {service_id} 缺少有效退款金额")
    order_item = next(
        (
            item
            for item in _order_items(order)
            if str(item.get("wareId") or "") == str(bill.get("wareId") or "")
        ),
        {},
    )
    combined_item = {**order_item, **bill}
    item = _item_from_order(combined_item, bill.get("serviceCount"))
    return NormalizedMarketplaceRefund(
        after_sales_sn=service_id,
        platform_order_sn=order_id,
        after_sales_type=AfterSalesType.RETURN_AND_REFUND,
        refund_amount=amount,
        platform_order_amount=money(order.get("orderPayment"), field="orderPayment"),
        platform_goods_amount=money(
            order.get("orderSellerPrice"), field="orderSellerPrice"
        ),
        buyer_reason_raw=nonempty(bill.get("applyReason") or bill.get("questionDesc")),
        buyer_memo=nonempty(bill.get("questionDesc")),
        product_name=item.product_name,
        platform_created_at=parse_datetime(bill.get("afsApplyTime")),
        platform_updated_at=parse_datetime(
            record.get("completeTime") or order.get("modified")
        ),
        forward_tracking_number=nonempty(order.get("waybill")),
        return_tracking_number=nonempty(
            record.get("expressNo") or bill.get("expressNo")
        ),
        carrier_code=nonempty(
            record.get("expressCompany") or order.get("logisticsId")
        ),
        order_shipping_status=_shipping_status(order),
        platform_after_sales_status_text=nonempty(record.get("status")),
        platform_order_status_text=nonempty(order.get("orderState")),
        items=(item,),
    )
