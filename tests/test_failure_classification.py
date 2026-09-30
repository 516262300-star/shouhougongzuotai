import json

import httpx
import pytest

from aftersales_workbench.integrations.marketplace.models import (
    MarketplaceApiError,
    MarketplaceTransportError,
)
from aftersales_workbench.workflows.failure_classification import (
    stage_has_technical_failure,
    technical_failure,
)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("timeout"),
        MarketplaceApiError("denied"),
        MarketplaceTransportError("invalid response"),
        KeyError("field"),
        TypeError("bad shape"),
        json.JSONDecodeError("invalid json", "", 0),
        TimeoutError(),
    ],
)
def test_technical_errors_are_never_business_blocked(error):
    assert technical_failure(error)


def test_wrapped_network_failure_is_still_technical():
    try:
        try:
            raise httpx.ReadTimeout("timeout")
        except httpx.ReadTimeout:
            raise ValueError("平台回查未完成") from None
    except ValueError as error:
        assert technical_failure(error)
    assert not technical_failure(ValueError("存在多个包裹，须人工核验"))


def test_stage_failure_does_not_depend_on_scanned_count_or_business_blocked():
    assert stage_has_technical_failure({"scanned": 20, "blocked": 19, "unavailable": 1})
    assert stage_has_technical_failure({"scanned": 0, "not_configured": 1})
    assert not stage_has_technical_failure({"scanned": 20, "blocked": 20, "unavailable": 0})
