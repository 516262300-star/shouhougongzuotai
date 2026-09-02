from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import SecretStr

from aftersales_workbench.db.models import AfterSalesType, Platform, ShippingStatus


class MarketplaceConfigurationError(ValueError):
    """多平台店铺配置缺失或不合法。"""


class MarketplaceApiError(RuntimeError):
    """平台 API 返回了明确的业务错误。"""


class MarketplaceTransportError(RuntimeError):
    """平台网关网络或响应协议异常。"""


@dataclass(frozen=True, slots=True)
class ConfiguredMarketplaceShop:
    platform: Platform
    shop_number: int
    shop_code: str
    shop_name: str
    platform_shop_id: str
    app_key: SecretStr
    app_secret: SecretStr
    access_token: SecretStr | None = None
    session_key: SecretStr | None = None
    access_token_mode: str = "static"


@dataclass(frozen=True, slots=True)
class NormalizedMarketplaceItem:
    sku_code: str
    applied_quantity: int
    product_name: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedMarketplaceRefund:
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
    order_shipping_status: ShippingStatus
    platform_after_sales_status_text: str | None
    platform_order_status_text: str | None
    items: tuple[NormalizedMarketplaceItem, ...]


@dataclass(slots=True)
class MarketplaceShopSyncResult:
    platform: str
    shop_number: int
    shop_code: str
    ok: bool
    windows: int = 0
    records_seen: int = 0
    records_created: int = 0
    records_updated: int = 0
    details_unavailable: int = 0
    error: str | None = None

    def safe_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "shop_number": self.shop_number,
            "shop_code": self.shop_code,
            "ok": self.ok,
            "windows": self.windows,
            "records_seen": self.records_seen,
            "records_created": self.records_created,
            "records_updated": self.records_updated,
            "details_unavailable": self.details_unavailable,
            "error": self.error,
        }
