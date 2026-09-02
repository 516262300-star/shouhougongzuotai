from __future__ import annotations

from typing import Any

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.marketplace.models import (
    ConfiguredMarketplaceShop,
    NormalizedMarketplaceItem,
    NormalizedMarketplaceRefund,
)
from aftersales_workbench.integrations.tmall.client import (
    TmallApiError,
    TmallClient,
    TmallCredentials,
)
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_refund,
    unwrap_refund,
    unwrap_seller,
    unwrap_trade,
)


class TaobaoReadClient:
    def __init__(
        self,
        config: ConfiguredMarketplaceShop,
        settings: Settings,
        *,
        client: TmallClient | None = None,
    ) -> None:
        if config.session_key is None:
            raise ValueError("淘宝店铺缺少 session_key")
        self.config = config
        self._client = client or TmallClient(
            TmallCredentials(
                shop_code=config.shop_code,
                app_key=config.app_key,
                app_secret=config.app_secret,
                session_key=config.session_key,
            ),
            api_url=settings.taobao_api_url,
            request_method=settings.taobao_request_method,
            timeout_seconds=settings.marketplace_timeout_seconds,
            read_max_attempts=settings.marketplace_read_max_attempts,
        )

    def __enter__(self) -> TaobaoReadClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self._client.close()

    def identity(self) -> tuple[str, str]:
        seller = unwrap_seller(self._client.get_seller())
        seller_id = str(seller.get("user_id") or seller.get("nick") or "").strip()
        seller_name = str(seller.get("nick") or "").strip()
        if not seller_id or not seller_name:
            raise ValueError("淘宝店铺信息缺少 user_id 或 nick")
        return seller_id, seller_name

    def fetch_window(
        self,
        *,
        start_modified_at: int,
        end_modified_at: int,
        page_size: int,
    ):
        from datetime import datetime

        page = 1
        trade_cache: dict[int, dict[str, Any]] = {}
        while True:
            body = self._client.get_refunds(
                start_modified=datetime.fromtimestamp(start_modified_at),
                end_modified=datetime.fromtimestamp(end_modified_at),
                page_no=page,
                page_size=min(page_size, 100),
            )
            payload = body.get("refunds_receive_get_response")
            if not isinstance(payload, dict):
                raise ValueError("淘宝退款列表缺少 refunds_receive_get_response")
            refunds_node = payload.get("refunds")
            records = refunds_node.get("refund") if isinstance(refunds_node, dict) else []
            records = records or []
            if not isinstance(records, list):
                raise ValueError("淘宝退款列表不是数组")
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("淘宝退款列表包含非对象记录")
                refund_id = int(record.get("refund_id") or 0)
                tid = int(record.get("tid") or 0)
                if refund_id < 1 or tid < 1:
                    raise ValueError("淘宝退款记录缺少 refund_id 或 tid")
                detail = unwrap_refund(self._client.get_refund(refund_id=refund_id))
                if tid not in trade_cache:
                    try:
                        trade_cache[tid] = unwrap_trade(
                            self._client.get_trade_fullinfo(tid=tid)
                        )
                    except TmallApiError as exc:
                        if exc.sub_code != "isv.trade-not-exist":
                            raise
                        trade_cache[tid] = {}
                normalized = normalize_refund(record, detail, trade_cache[tid])
                yield NormalizedMarketplaceRefund(
                    after_sales_sn=normalized.after_sales_sn,
                    platform_order_sn=normalized.platform_order_sn,
                    after_sales_type=normalized.after_sales_type,
                    refund_amount=normalized.refund_amount,
                    platform_order_amount=normalized.platform_order_amount,
                    platform_goods_amount=normalized.platform_goods_amount,
                    buyer_reason_raw=normalized.buyer_reason_raw,
                    buyer_memo=normalized.buyer_memo,
                    product_name=normalized.product_name,
                    platform_created_at=normalized.platform_created_at,
                    platform_updated_at=normalized.platform_updated_at,
                    forward_tracking_number=None,
                    return_tracking_number=normalized.return_tracking_number,
                    carrier_code=normalized.carrier_code,
                    order_shipping_status=normalized.order_shipping_status,
                    platform_after_sales_status_text=str(
                        detail.get("status") or record.get("status") or ""
                    )
                    or None,
                    platform_order_status_text=str(
                        trade_cache[tid].get("status") or ""
                    )
                    or None,
                    items=(
                        NormalizedMarketplaceItem(
                            sku_code=normalized.item.sku_code,
                            applied_quantity=normalized.item.applied_quantity,
                            product_name=normalized.product_name,
                        ),
                    ),
                )
            if payload.get("has_next") is False or len(records) < min(page_size, 100):
                break
            page += 1
            if page > 1000:
                raise ValueError("淘宝退款分页超过 1000 页")
