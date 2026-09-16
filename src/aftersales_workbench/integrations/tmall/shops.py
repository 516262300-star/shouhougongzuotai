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
    refund_session_key: SecretStr | None = None

    def credentials(self) -> TmallCredentials:
        return TmallCredentials(
            shop_code=self.shop_code,
            app_key=self.app_key,
            app_secret=self.app_secret,
            session_key=self.session_key,
        )

    def refund_credentials(self) -> TmallCredentials:
        if not _secret_text(self.refund_session_key):
            raise TmallConfigurationError(
                f"店铺 {self.shop_code} 未配置退款子账号 SessionKey"
            )
        return TmallCredentials(
            shop_code=self.shop_code,
            app_key=self.app_key,
            app_secret=self.app_secret,
            session_key=self.refund_session_key,  # type: ignore[arg-type]
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
    seen_refund_sessions: set[str] = set()
    for shop_number in range(1, 7):
        shop_code = str(getattr(settings, f"tmall_shop_{shop_number}_code")).strip()
        session_key = getattr(settings, f"tmall_shop_{shop_number}_session_key")
        refund_session_key = getattr(
            settings, f"tmall_shop_{shop_number}_refund_session_key"
        )
        session_text = _secret_text(session_key)
        refund_session_text = _secret_text(refund_session_key)
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
        if refund_session_text and refund_session_text in seen_refund_sessions:
            raise TmallConfigurationError(
                f"天猫 {shop_number} 店与其他店使用了相同退款 SessionKey"
            )
        if refund_session_text and refund_session_text == session_text:
            raise TmallConfigurationError(
                f"天猫 {shop_number} 店的主账号与退款子账号 SessionKey 不能相同"
            )
        shops.append(
            ConfiguredTmallShop(
                shop_number=shop_number,
                shop_code=shop_code,
                app_key=app_key,  # type: ignore[arg-type]
                app_secret=app_secret,  # type: ignore[arg-type]
                session_key=session_key,
                refund_session_key=refund_session_key,
            )
        )
        seen_codes.add(shop_code)
        seen_sessions.add(session_text)
        if refund_session_text:
            seen_refund_sessions.add(refund_session_text)

    if missing:
        raise TmallConfigurationError("缺少多店配置: " + ", ".join(sorted(set(missing))))
    if not shops:
        raise TmallConfigurationError("没有可用的天猫店铺配置")
    return shops


def load_refund_enabled_tmall_shops(settings: Settings) -> list[ConfiguredTmallShop]:
    numbers = tuple(settings.tmall_refund_enabled_shop_numbers)
    if len(numbers) != len(set(numbers)):
        raise TmallConfigurationError("TMALL_REFUND_ENABLED_SHOP_NUMBERS 不能重复")
    invalid = sorted(number for number in numbers if number < 1 or number > 6)
    if invalid:
        raise TmallConfigurationError(
            "TMALL_REFUND_ENABLED_SHOP_NUMBERS 只能包含 1–6"
        )
    if not numbers:
        return []
    configured = {
        shop.shop_number: shop
        for shop in load_configured_tmall_shops(settings, require_all=False)
    }
    missing = [
        number
        for number in numbers
        if number not in configured
        or not _secret_text(configured[number].refund_session_key)
    ]
    if missing:
        labels = ", ".join(
            f"TMALL_SHOP_{number}_REFUND_SESSION_KEY" for number in missing
        )
        raise TmallConfigurationError("退款白名单店铺缺少配置: " + labels)
    return [configured[number] for number in numbers]
