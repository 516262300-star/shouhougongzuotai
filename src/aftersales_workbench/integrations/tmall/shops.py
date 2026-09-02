from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.tmall.client import (
    TmallConfigurationError,
    TmallCredentials,
)


@dataclass(frozen=True, slots=True)
class ConfiguredTmallShop:
    shop_number: int
    shop_code: str
    app_key: SecretStr
    app_secret: SecretStr
    session_key: SecretStr

    def credentials(self) -> TmallCredentials:
        return TmallCredentials(
            shop_code=self.shop_code,
            app_key=self.app_key,
            app_secret=self.app_secret,
            session_key=self.session_key,
        )


def _secret_text(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value else ""


def load_configured_tmall_shops(
    settings: Settings,
    *,
    require_all: bool = True,
) -> list[ConfiguredTmallShop]:
    app_key = settings.tmall_app_key
    app_secret = settings.tmall_app_secret
    common_missing = []
    if not _secret_text(app_key):
        common_missing.append("TMALL_APP_KEY")
    if not _secret_text(app_secret):
        common_missing.append("TMALL_APP_SECRET")
    if common_missing:
        raise TmallConfigurationError("缺少环境变量: " + ", ".join(common_missing))

    shops: list[ConfiguredTmallShop] = []
    missing: list[str] = []
    seen_codes: set[str] = set()
    seen_sessions: set[str] = set()
    for shop_number in range(1, 7):
        shop_code = str(getattr(settings, f"tmall_shop_{shop_number}_code")).strip()
        session_key = getattr(settings, f"tmall_shop_{shop_number}_session_key")
        session_text = _secret_text(session_key)
        if not session_text:
            if require_all:
                missing.append(f"TMALL_SHOP_{shop_number}_SESSION_KEY")
            continue
        if not shop_code:
            missing.append(f"TMALL_SHOP_{shop_number}_CODE")
            continue
        if shop_code in seen_codes:
            raise TmallConfigurationError(f"店铺代号重复: {shop_code}")
        if session_text in seen_sessions:
            raise TmallConfigurationError(f"天猫 {shop_number} 店与其他店使用了相同 SessionKey")
        shops.append(
            ConfiguredTmallShop(
                shop_number=shop_number,
                shop_code=shop_code,
                app_key=app_key,  # type: ignore[arg-type]
                app_secret=app_secret,  # type: ignore[arg-type]
                session_key=session_key,
            )
        )
        seen_codes.add(shop_code)
        seen_sessions.add(session_text)

    if missing:
        raise TmallConfigurationError("缺少多店配置: " + ", ".join(sorted(set(missing))))
    if not shops:
        raise TmallConfigurationError("没有可用的天猫店铺配置")
    return shops
