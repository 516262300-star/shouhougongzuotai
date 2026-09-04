from __future__ import annotations

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.tmall.client import TmallConfigurationError
from aftersales_workbench.integrations.tmall.shops import (
    load_refund_enabled_tmall_shops,
)


def test_refund_enabled_loader_returns_only_whitelisted_shops() -> None:
    settings = Settings(
        _env_file=None,
        tmall_app_key="app-key",
        tmall_app_secret="app-secret",
        tmall_shop_1_session_key="main-1",
        tmall_shop_1_refund_session_key="refund-1",
        tmall_shop_2_session_key="main-2",
        tmall_shop_2_refund_session_key="refund-2",
        tmall_refund_enabled_shop_numbers=[1, 2],
    )

    shops = load_refund_enabled_tmall_shops(settings)

    assert [shop.shop_number for shop in shops] == [1, 2]
    assert shops[0].refund_credentials().session_key.get_secret_value() == "refund-1"


def test_refund_enabled_loader_rejects_missing_child_session() -> None:
    settings = Settings(
        _env_file=None,
        tmall_app_key="app-key",
        tmall_app_secret="app-secret",
        tmall_shop_1_session_key="main-1",
        tmall_refund_enabled_shop_numbers=[1],
    )

    with pytest.raises(TmallConfigurationError, match="REFUND_SESSION_KEY"):
        load_refund_enabled_tmall_shops(settings)
