from __future__ import annotations

from collections.abc import Iterable

from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import Platform
from aftersales_workbench.integrations.marketplace.models import (
    ConfiguredMarketplaceShop,
    MarketplaceConfigurationError,
)

_PLATFORM_FIELDS = {
    Platform.TAOBAO: "taobao_shops_json",
    Platform.ALIBABA_1688: "alibaba_1688_shops_json",
    Platform.JD: "jd_shops_json",
    Platform.DOUYIN: "douyin_shops_json",
}
_TOKEN_FIELD = {
    Platform.TAOBAO: "session_key",
    Platform.ALIBABA_1688: None,
    Platform.JD: "access_token",
    Platform.DOUYIN: "access_token",
}


def _required_text(entry: dict[str, str], field: str, label: str) -> str:
    value = str(entry.get(field) or "").strip()
    if not value:
        raise MarketplaceConfigurationError(f"{label} 缺少 {field}")
    return value


def load_marketplace_shops(
    settings: Settings,
    platform: Platform,
) -> list[ConfiguredMarketplaceShop]:
    try:
        entries: Iterable[dict[str, str]] = getattr(settings, _PLATFORM_FIELDS[platform])
    except KeyError as exc:
        raise MarketplaceConfigurationError(f"暂不支持平台 {platform.value}") from exc
    shops: list[ConfiguredMarketplaceShop] = []
    seen_codes: set[str] = set()
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise MarketplaceConfigurationError(
                f"{platform.value} 第 {number} 个店铺配置必须是 JSON 对象"
            )
        label = f"{platform.value} 第 {number} 店"
        shop_code = _required_text(entry, "shop_code", label)
        if shop_code in seen_codes:
            raise MarketplaceConfigurationError(f"店铺代号重复: {shop_code}")
        app_key = _required_text(entry, "app_key", label)
        app_secret = _required_text(entry, "app_secret", label)
        token_field = _TOKEN_FIELD[platform]
        token = _required_text(entry, token_field, label) if token_field else ""
        shops.append(
            ConfiguredMarketplaceShop(
                platform=platform,
                shop_number=number,
                shop_code=shop_code,
                shop_name=str(entry.get("shop_name") or shop_code).strip(),
                platform_shop_id=str(
                    entry.get("platform_shop_id") or shop_code
                ).strip(),
                app_key=SecretStr(app_key),
                app_secret=SecretStr(app_secret),
                access_token=(
                    SecretStr(token) if token_field == "access_token" else None
                ),
                session_key=(
                    SecretStr(token) if token_field == "session_key" else None
                ),
            )
        )
        seen_codes.add(shop_code)
    if not shops:
        raise MarketplaceConfigurationError(f"{platform.value} 没有配置店铺")
    return shops
