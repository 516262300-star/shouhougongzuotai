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
        self._shop_name = config.shop_name

    def identity(self) -> tuple[str, str]:
        self._effective_access_token()
        return self.config.platform_shop_id, self._shop_name

    def _request(
        self,
        path: str,
        parameters: dict[str, Any],
        *,
        access_token: str,
    ) -> dict[str, Any]:
        if path not in {
            DOUYIN_TOKEN_CREATE_PATH, DOUYIN_AFTERSALE_LIST_PATH, DOUYIN_AFTERSALE_DETAIL_PATH,
        }:
            raise ValueError("抖音同步禁止调用写业务接口")
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
        if code not in (0, 10000, "0", "10000"):
            raise MarketplaceApiError(
                "抖音 API error: "
                f"code={code}, sub_code={body.get('sub_code')}"
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
        if (str(entry.get("shop_id")) != self.config.platform_shop_id
                or entry.get("app_key") != self.config.app_key.get_secret_value()):
            return ""
        token = str(entry.get("access_token") or "").strip()
        expires_at = int(entry.get("expires_at") or 0)
        if expires_at <= int(self._now()) + self.token_refresh_skew_seconds:
            return ""
        self._shop_name = str(entry.get("shop_name") or self.config.shop_name)
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
            "shop_id": self.config.platform_shop_id,
            "shop_name": self._shop_name,
            "app_key": self.config.app_key.get_secret_value(),
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
        if str(data.get("shop_id")) != self.config.platform_shop_id:
            raise MarketplaceApiError("抖音授权返回店铺身份不一致")
        self._shop_name = str(data.get("shop_name") or self.config.shop_name)
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
        if path not in {DOUYIN_AFTERSALE_LIST_PATH, DOUYIN_AFTERSALE_DETAIL_PATH}:
            raise ValueError("抖音同步仅允许售后只读接口")
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
        seen: set[str] = set()
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
            raw = data.get("items")
            if raw is None and data.get("total") == 0:
                raw = []
            if not isinstance(raw, list):
                raise ValueError("抖音售后列表缺少明确 items/total，禁止推进游标")
            records = list_of_mappings(raw)
            for record in records:
                info = record.get("aftersale_info")
                if not isinstance(info, dict):
                    raise ValueError("抖音售后记录缺少 aftersale_info")
                after_sales_id = required_text(
                    info.get("aftersale_id"), field="aftersale_id"
                )
                if after_sales_id in seen:
                    raise ValueError("抖音分页重复售后，禁止推进游标")
                seen.add(after_sales_id)
                detail_body = self.get_detail(after_sales_id)
                detail_data = detail_body.get("data")
                if not isinstance(detail_data, dict):
                    raise ValueError("抖音售后详情缺少 data")
                detail = detail_data
                yield normalize_douyin_refund(record, detail)
            if len(records) < limit:
                if int(data.get("total") or 0) > len(seen):
                    raise ValueError("抖音售后分页提前结束，禁止推进游标")
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
    process = detail.get("process_info") or {}
    logistics = (process.get("logistics_info") or {}).get("order")
    if isinstance(logistics, list):
        numbers = {str(x["tracking_no"]).strip() for x in logistics if x.get("tracking_no")}
        # 多包裹不擅自选择其中一单充当整单运单。
        if len(numbers) == 1:
            return numbers.pop()
        if len(numbers) > 1:
            return None
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
    process = detail.get("process_info") or {}
    detail_info = process.get("after_sale_info") or {}
    detail_order = detail.get("order_info") or {}
    if str(detail_info.get("after_sale_id")) != after_sales_id:
        raise ValueError("抖音售后详情与列表单号不一致")
    if str(detail_order.get("shop_order_id")) != order_id:
        raise ValueError("抖音售后详情与列表订单不一致")
    kind = str(detail_info.get("after_sale_type"))
    kinds = {
        "0": AfterSalesType.RETURN_AND_REFUND,
        "1": AfterSalesType.ONLY_REFUND, "2": AfterSalesType.ONLY_REFUND,
        "3": AfterSalesType.EXCHANGE, "4": AfterSalesType.ONLY_REFUND,
        "5": AfterSalesType.ONLY_REFUND, "6": AfterSalesType.ONLY_REFUND,
        "7": AfterSalesType.RESEND, "8": AfterSalesType.REPAIR,
    }
    if kind not in kinds:
        raise ValueError("抖音未知售后类型，待核对")
    amount = money(
        detail_info.get("refund_total_amount"), field="refund_total_amount", divisor=100,
    )
    if amount is None:
        raise ValueError(f"抖音售后 {after_sales_id} 缺少有效退款金额")
    sku_infos = (
        detail_order.get("sku_order_infos")
        if isinstance(detail_order, dict)
        else None
    )
    items_by_code: dict[str, NormalizedMarketplaceItem] = {}
    for item in list_of_mappings(sku_infos):
        raw_quantity = item.get("after_sale_item_count")
        if raw_quantity is None or not str(raw_quantity).isdigit():
            raise ValueError("抖音缺少明确的商品售后数量")
        quantity = int(raw_quantity)
        if quantity == 0:
            continue
        code = required_text(
            item.get("shop_sku_code") or item.get("sku_id"), field="shop_sku_code/sku_id",
        )
        previous = items_by_code.get(code)
        items_by_code[code] = NormalizedMarketplaceItem(
            sku_code=code,
            applied_quantity=quantity + (previous.applied_quantity if previous else 0),
            product_name=nonempty(item.get("product_name") or item.get("goods_name")),
        )
    items = list(items_by_code.values())
    if not items:
        raise ValueError("抖音售后缺少正数量商品明细")
    expected = detail_info.get("after_sale_apply_count")
    if expected is not None and sum(x.applied_quantity for x in items) != int(expected):
        raise ValueError("抖音售后总数量与商品明细不一致")
    got_pkg = str(detail_info.get("got_pkg")) == "1"
    forward_tracking = _forward_logistics(record, detail)
    return_tracking, return_carrier = _return_logistics(detail)
    # 售后详情只含相关商品，不能把子单金额冒充整张父订单的支付总额。
    order_amount_raw = order_info.get("pay_amount")
    status_text = nonempty(
        detail_info.get("after_sale_status_desc") or text_part.get("aftersale_status_text")
    )
    status = str(detail_info.get("after_sale_status"))
    refund_status = str(detail_info.get("refund_status"))
    financial_status, actual, completed = "UNKNOWN", None, None
    if kind in {"3", "7", "8"}:
        financial_status = "NOT_APPLICABLE"
    elif refund_status == "3":
        actual = money(
            detail_info.get("real_refund_amount"), field="real_refund_amount", divisor=100,
        )
        refunded_at = detail_info.get("refund_time")
        if actual is None or not refunded_at or int(refunded_at) <= 0:
            raise ValueError("抖音退款成功缺少明确实退金额/完成时间")
        completed = parse_datetime(refunded_at)
        financial_status = "SUCCESS"
    elif status == "28" or (status == "51" and refund_status == "0"):
        financial_status = "CLOSED"
    elif refund_status in {"1", "2", "4"}:
        financial_status = "PENDING"
    shipping = ShippingStatus.UNKNOWN
    if got_pkg:
        shipping = ShippingStatus.DELIVERED
    elif forward_tracking or kind == "1":
        shipping = ShippingStatus.IN_TRANSIT
    elif kind == "2":
        shipping = ShippingStatus.UNSHIPPED
    return NormalizedMarketplaceRefund(
        after_sales_sn=after_sales_id,
        platform_order_sn=order_id,
        after_sales_type=kinds[kind],
        refund_amount=amount,
        platform_order_amount=money(
            order_amount_raw, field="order_info.pay_amount", divisor=100
        ),
        platform_goods_amount=None,
        buyer_reason_raw=nonempty(detail_info.get("reason") or text_part.get("reason_text")),
        buyer_memo=nonempty(
            detail_info.get("reason_remark") or text_part.get("description")
        ),
        product_name=next(
            (item.product_name for item in items if item.product_name), None
        ),
        platform_created_at=parse_datetime(
            info.get("create_time") or info.get("apply_time")
        ),
        platform_updated_at=parse_datetime(detail_info.get("update_time")),
        forward_tracking_number=forward_tracking,
        return_tracking_number=return_tracking,
        carrier_code=return_carrier,
        order_shipping_status=shipping,
        platform_after_sales_status_text=status_text,
        platform_order_status_text=nonempty(order_info.get("order_status")),
        items=tuple(items),
        refund_financial_status=financial_status,
        actual_refund_amount=actual,
        refund_completed_at=completed,
    )
