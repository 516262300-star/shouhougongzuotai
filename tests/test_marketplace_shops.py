from __future__ import annotations

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import Platform
from aftersales_workbench.integrations.marketplace.models import (
    MarketplaceConfigurationError,
)
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops


def test_load_taobao_shops_from_json_settings() -> None:
    settings = Settings(
        _env_file=None,
        taobao_shops_json=[
            {
                "shop_code": "taobao-01",
                "shop_name": "淘宝一店",
                "app_key": "key",
                "app_secret": "secret",
                "session_key": "session",
            }
        ],
    )

    shops = load_marketplace_shops(settings, Platform.TAOBAO)

    assert shops[0].shop_name == "淘宝一店"
    assert shops[0].session_key is not None
    assert shops[0].session_key.get_secret_value() == "session"


def test_load_douyin_requires_access_token() -> None:
    settings = Settings(
        _env_file=None,
        douyin_shops_json=[
            {"shop_code": "douyin-01", "app_key": "key", "app_secret": "secret"}
        ],
    )

    with pytest.raises(MarketplaceConfigurationError, match="access_token"):
        load_marketplace_shops(settings, Platform.DOUYIN)


def test_load_douyin_allows_third_party_self_authorization() -> None:
    settings = Settings(
        _env_file=None,
        douyin_shops_json=[
            {
                "shop_code": "douyin-01",
                "platform_shop_id": "123456",
                "app_key": "key",
                "app_secret": "secret",
                "access_token_mode": "authorization_self",
            }
        ],
    )

    shops = load_marketplace_shops(settings, Platform.DOUYIN)

    assert shops[0].access_token is None
    assert shops[0].access_token_mode == "authorization_self"
