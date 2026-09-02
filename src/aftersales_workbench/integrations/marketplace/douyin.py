from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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

DOUYIN_AFTERSALE_LIST_PATH = "/afterSale/List"
DOUYIN_AFTERSALE_DETAIL_PATH = "/afterSale/Detail"
DOUYIN_TOKEN_CREATE_PATH = "/token/create"


def _sort_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sort_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_json(item) for item in value]
    return value


def marshal_douyin_parameters(parameters: dict[str, Any]) -> str:
    return json.dumps(
        _sort_json(parameters),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def generate_douyin_sign(
    *,
    app_key: str,
    app_secret: str,
    method: str,
    timestamp: int,
    param_json: str,
) -> str:
    pattern = (
        f"app_key{app_key}method{method}param_json{param_json}"
        f"timestamp{timestamp}v2"
    )
    source = f"{app_secret}{pattern}{app_secret}"
    return hmac.new(
        app_secret.encode("utf-8"),
        source.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class DouyinReadClient(RetryingJsonClient):
    def __init__(
        self,
        config: ConfiguredMarketplaceShop,
        settings: Settings,
        *,
        now=time.time,
        **kwargs: Any,
    ) -> None:
        if config.access_token is None and config.access_token_mode != "authorization_self":
            raise ValueError("抖音店铺缺少 access_token")
        super().__init__(
            timeout_seconds=settings.marketplace_timeout_seconds,
            read_max_attempts=settings.marketplace_read_max_attempts,
            **kwargs,
        )
        self.config = config
        self.api_url = settings.douyin_api_url.rstrip("/")
        self.token_cache_path = Path(settings.douyin_token_cache_path)
        self.token_refresh_skew_seconds = settings.douyin_token_refresh_skew_seconds
        self._now = now
        self._access_token = (
            config.access_token.get_secret_value().strip()
            if config.access_token is not None
            else ""
        )

    def identity(self) -> tuple[str, str]:
        return self.config.platform_shop_id, self.config.shop_name

    def _request(
        self,
        path: str,
        parameters: dict[str, Any],
        *,
        access_token: str,
    ) -> dict[str, Any]:
        app_key = self.config.app_key.get_secret_value().strip()
        app_secret = self.config.app_secret.get_secret_value().strip()
        timestamp = int(self._now())
        method = path.lstrip("/").replace("/", ".")
        param_json = marshal_douyin_parameters(parameters)
        query = {
            "app_key": app_key,
            "method": method,
            "v": "2",
            "sign": generate_douyin_sign(
                app_key=app_key,
                app_secret=app_secret,
                method=method,
                timestamp=timestamp,
                param_json=param_json,
            ),
            "timestamp": timestamp,
            "access_token": access_token,
            "sign_method": "hmac-sha256",
        }
        body = self.request_json(
            "POST",
            f"{self.api_url}{path}?{urlencode(query)}",
            content=param_json.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        code = body.get("code")
        if code not in (None, 0, 10000, "0", "10000"):
            raise MarketplaceApiError(
                "抖音 API error: "
                f"code={code}, message={body.get('msg') or body.get('message')}"
            )
        return body

    def _read_cached_access_token(self) -> str:
        try:
            body = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return ""
        entry = body.get(self.config.shop_code) if isinstance(body, dict) else None
        if not isinstance(entry, dict):
            return ""
        token = str(entry.get("access_token") or "").strip()
        expires_at = int(entry.get("expires_at") or 0)
        if expires_at <= int(self._now()) + self.token_refresh_skew_seconds:
            return ""
        return token

    def _write_cached_access_token(self, token: str, expires_at: int) -> None:
        try:
            body = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        body[self.config.shop_code] = {
            "access_token": token,
            "expires_at": expires_at,
        }
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_cache_path.with_suffix(
            self.token_cache_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.token_cache_path)

    def _self_authorized_access_token(self) -> str:
        cached = self._read_cached_access_token()
        if cached:
            return cached
        shop_id_text = self.config.platform_shop_id.strip()
        shop_id: int | str = (
            int(shop_id_text) if shop_id_text.isdigit() else shop_id_text
        )
        body = self._request(
            DOUYIN_TOKEN_CREATE_PATH,
            {
                "grant_type": "authorization_self",
                "shop_id": shop_id,
                "code": "",
            },
            access_token="",
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise MarketplaceApiError("抖音自授权返回缺少 data")
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise MarketplaceApiError("抖音自授权返回缺少 access_token")
        try:
            expires_in = int(data.get("expires_in") or 604800)
        except (TypeError, ValueError):
            expires_in = 604800
        expires_at = int(self._now()) + max(expires_in, 300)
        self._write_cached_access_token(token, expires_at)
        return token

    def _effective_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        self._access_token = self._self_authorized_access_token()
        return self._access_token

    def execute_read(self, path: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            path,
            parameters,
            access_token=self._effective_access_token(),
        )

    def get_detail(self, after_sales_id: str) -> dict[str, Any]:
        return self.execute_read(
            DOUYIN_AFTERSALE_DETAIL_PATH,
            {"after_sale_id": after_sales_id},
        )

    def fetch_window(
        self,
        *,
        start_modified_at: int,
        end_modified_at: int,
        page_size: int,
    ):
        limit = min(page_size, 100)
        page = 0
        while True:
            body = self.execute_read(
                DOUYIN_AFTERSALE_LIST_PATH,
                {
                    "page": page,
                    "size": limit,
                    "update_start_time": start_modified_at,
                    "update_end_time": end_modified_at,
                },
            )
            data = body.get("data")
            if not isinstance(data, dict):
                raise ValueError("抖音售后列表缺少 data")
            records = list_of_mappings(data.get("items"))
            for record in records:
                info = record.get("aftersale_info")
                if not isinstance(info, dict):
                    raise ValueError("抖音售后记录缺少 aftersale_info")
                after_sales_id = required_text(
                    info.get("aftersale_id"), field="aftersale_id"
                )
                detail_body = self.get_detail(after_sales_id)
                detail_data = detail_body.get("data")
                detail = detail_data if isinstance(detail_data, dict) else {}
                yield normalize_douyin_refund(record, detail)
            if len(records) < limit:
                break
            page += 1
            if page > 1000:
                raise ValueError("抖音售后分页超过 1000 页")


def _return_logistics(detail: dict[str, Any]) -> tuple[str | None, str | None]:
    process = detail.get("process_info")
    logistics = process.get("logistics_info") if isinstance(process, dict) else None
    returned = logistics.get("return") if isinstance(logistics, dict) else None
    if not isinstance(returned, dict):
        return None, None
    return (
        nonempty(
            returned.get("tracking_no")
            or returned.get("logistics_code")
            or returned.get("logistics_no")
        ),
        nonempty(returned.get("company_name") or returned.get("company_code")),
    )


def _forward_logistics(record: dict[str, Any], detail: dict[str, Any]) -> str | None:
    order_info = detail.get("order_info")
    if not isinstance(order_info, dict):
        order_info = record.get("order_info")
    if not isinstance(order_info, dict):
        return None
    return nonempty(
        order_info.get("logistics_tracking_no")
        or order_info.get("tracking_no")
        or order_info.get("logistics_code")
    )


def normalize_douyin_refund(
    record: dict[str, Any],
    detail: dict[str, Any],
) -> NormalizedMarketplaceRefund:
    info = record.get("aftersale_info")
    order_info = record.get("order_info")
    text_part = record.get("text_part")
    if not isinstance(info, dict) or not isinstance(order_info, dict):
        raise ValueError("抖音售后记录结构不完整")
    if not isinstance(text_part, dict):
        text_part = {}
    after_sales_id = required_text(info.get("aftersale_id"), field="aftersale_id")
    order_id = required_text(order_info.get("shop_order_id"), field="shop_order_id")
    amount = money(info.get("refund_amount"), field="refund_amount", divisor=100)
    if amount is None or amount <= 0:
        raise ValueError(f"抖音售后 {after_sales_id} 缺少有效退款金额")

    detail_order = detail.get("order_info")
    sku_infos = (
        detail_order.get("sku_order_infos")
        if isinstance(detail_order, dict)
        else None
    )
    items: list[NormalizedMarketplaceItem] = []
    for item in list_of_mappings(sku_infos):
        items.append(
            NormalizedMarketplaceItem(
                sku_code=(
                    nonempty(item.get("shop_sku_code"))
                    or nonempty(item.get("sku_id"))
                    or nonempty(item.get("sku_order_id"))
                    or after_sales_id
                ),
                applied_quantity=positive_int(
                    item.get("after_sale_item_count")
                    or item.get("item_quantity"),
                    field="after_sale_item_count",
                ),
                product_name=nonempty(
                    item.get("product_name") or item.get("goods_name")
                ),
            )
        )
    if not items:
        items.append(
            NormalizedMarketplaceItem(
                sku_code=str(info.get("related_id") or after_sales_id),
                applied_quantity=positive_int(
                    info.get("aftersale_num"), field="aftersale_num"
                ),
            )
        )
    got_pkg = str(info.get("got_pkg") or "0") in {"1", "true", "True"}
    forward_tracking = _forward_logistics(record, detail)
    return_tracking, return_carrier = _return_logistics(detail)
    order_amount_raw = (
        order_info.get("pay_amount")
        or order_info.get("order_amount")
        or order_info.get("total_amount")
    )
    status_text = nonempty(
        text_part.get("aftersale_status_text") or info.get("aftersale_status")
    )
    return NormalizedMarketplaceRefund(
        after_sales_sn=after_sales_id,
        platform_order_sn=order_id,
        after_sales_type=(
            AfterSalesType.RETURN_AND_REFUND if got_pkg else AfterSalesType.ONLY_REFUND
        ),
        refund_amount=amount,
        platform_order_amount=money(
            order_amount_raw, field="order_info.pay_amount", divisor=100
        ),
        platform_goods_amount=None,
        buyer_reason_raw=nonempty(text_part.get("reason_text")),
        buyer_memo=nonempty(
            text_part.get("description") or info.get("aftersale_reason")
        ),
        product_name=next(
            (item.product_name for item in items if item.product_name), None
        ),
        platform_created_at=parse_datetime(
            info.get("create_time") or info.get("apply_time")
        ),
        platform_updated_at=parse_datetime(info.get("update_time")),
        forward_tracking_number=forward_tracking,
        return_tracking_number=return_tracking,
        carrier_code=return_carrier,
        order_shipping_status=(
            ShippingStatus.DELIVERED
            if got_pkg
            else (ShippingStatus.IN_TRANSIT if forward_tracking else ShippingStatus.UNSHIPPED)
        ),
        platform_after_sales_status_text=status_text,
        platform_order_status_text=nonempty(order_info.get("order_status")),
        items=tuple(items),
    )
