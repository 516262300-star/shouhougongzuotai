from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.pdd.client import (
    PddConfigurationError,
    PddCredentials,
)


@dataclass(frozen=True, slots=True)
class ConfiguredPddShop:
    shop_number: int
    app_group: int
    shop_code: str
    client_id: SecretStr
    client_secret: SecretStr
    access_token: SecretStr

    def credentials(self) -> PddCredentials:
        return PddCredentials(
            shop_code=self.shop_code,
            client_id=self.client_id,
            client_secret=self.client_secret,
            access_token=self.access_token,
        )


def _secret_text(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value else ""


def load_configured_pdd_shops(
    settings: Settings,
    *,
    require_all: bool = True,
) -> list[ConfiguredPddShop]:
    shops: list[ConfiguredPddShop] = []
    missing: list[str] = []
    seen_codes: set[str] = set()
    seen_tokens: set[str] = set()

    for shop_number in range(1, 8):
        shop_code = str(getattr(settings, f"pdd_shop_{shop_number}_code")).strip()
        app_group = int(getattr(settings, f"pdd_shop_{shop_number}_app"))
        client_id = getattr(settings, f"pdd_app_{app_group}_client_id")
        client_secret = getattr(settings, f"pdd_app_{app_group}_client_secret")
        access_token = getattr(settings, f"pdd_shop_{shop_number}_access_token")

        required = {
            f"PDD_APP_{app_group}_CLIENT_ID": _secret_text(client_id),
            f"PDD_APP_{app_group}_CLIENT_SECRET": _secret_text(client_secret),
            f"PDD_SHOP_{shop_number}_ACCESS_TOKEN": _secret_text(access_token),
        }
        absent = [name for name, value in required.items() if not value]
        if absent:
            if require_all:
                missing.extend(absent)
            continue
        if not shop_code:
            missing.append(f"PDD_SHOP_{shop_number}_CODE")
            continue
        if shop_code in seen_codes:
            raise PddConfigurationError(f"店铺代号重复: {shop_code}")
        token_text = _secret_text(access_token)
        if token_text in seen_tokens:
            raise PddConfigurationError(f"店铺 {shop_number} 与其他店铺使用了相同 Token")

        shops.append(
            ConfiguredPddShop(
                shop_number=shop_number,
                app_group=app_group,
                shop_code=shop_code,
                client_id=client_id,
                client_secret=client_secret,
                access_token=access_token,
            )
        )
        seen_codes.add(shop_code)
        seen_tokens.add(token_text)

    if missing:
        raise PddConfigurationError("缺少多店配置: " + ", ".join(sorted(set(missing))))
    if not shops:
        raise PddConfigurationError("没有可用的拼多多店铺配置")
    return shops
