import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.pdd.client import PddConfigurationError
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "pdd_app_1_client_id": "app-1",
        "pdd_app_1_client_secret": "secret-1",
        "pdd_app_2_client_id": "app-2",
        "pdd_app_2_client_secret": "secret-2",
    }
    for shop_number in range(1, 8):
        values[f"pdd_shop_{shop_number}_access_token"] = f"token-{shop_number}"
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_load_seven_shops_uses_expected_app_groups() -> None:
    shops = load_configured_pdd_shops(_settings())

    assert len(shops) == 7
    assert [shop.app_group for shop in shops] == [1, 1, 1, 1, 2, 2, 2]
    assert shops[0].client_id.get_secret_value() == "app-1"
    assert shops[6].client_id.get_secret_value() == "app-2"


def test_duplicate_shop_token_is_rejected() -> None:
    settings = _settings(pdd_shop_7_access_token="token-1")

    with pytest.raises(PddConfigurationError, match="相同 Token"):
        load_configured_pdd_shops(settings)
