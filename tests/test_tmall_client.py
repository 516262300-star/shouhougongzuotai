from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.integrations.tmall.client import (
    TAOBAO_REFUNDS_RECEIVE_GET,
    TmallApiError,
    TmallClient,
    TmallCredentials,
    generate_sign,
)


@pytest.fixture
def credentials() -> TmallCredentials:
    return TmallCredentials(
        shop_code="tmall-shop-01",
        app_key=SecretStr("app-key"),
        app_secret=SecretStr("app-secret"),
        session_key=SecretStr("session-key"),
    )


def test_generate_sign_is_stable_and_uppercase() -> None:
    assert generate_sign({"b": "2", "a": "1"}, "secret") == (
        "EF16F26C937CF52AE6F85DF2FD08B24A"
    )


def test_signed_payload_never_contains_app_secret(credentials: TmallCredentials) -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = TmallClient(credentials, http_client=http_client, now=lambda: 1_700_000_000)

    payload = client.build_signed_payload("taobao.test", {"flag": True})

    assert payload["method"] == "taobao.test"
    assert payload["session"] == "session-key"
    assert payload["flag"] == "true"
    assert "app_secret" not in payload
    assert "app-secret" not in json.dumps(payload)
    http_client.close()


def test_refund_list_calls_official_method(credentials: TmallCredentials) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200,
            json={"refunds_receive_get_response": {"refunds": {"refund": []}}},
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TmallClient(credentials, http_client=http_client)
    from datetime import datetime

    client.get_refunds(
        start_modified=datetime(2026, 9, 1),
        end_modified=datetime(2026, 9, 2),
    )

    assert captured["method"] == TAOBAO_REFUNDS_RECEIVE_GET
    assert captured["start_modified"] == "2026-09-01 00:00:00"
    assert captured["use_has_next"] == "true"
    http_client.close()


def test_api_error_preserves_safe_diagnostics(credentials: TmallCredentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error_response": {
                    "code": 27,
                    "msg": "Invalid session",
                    "sub_code": "invalid-sessionkey",
                    "request_id": "request-1",
                }
            },
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TmallClient(credentials, http_client=http_client)

    with pytest.raises(TmallApiError) as caught:
        client.get_seller()

    assert caught.value.code == 27
    assert caught.value.request_id == "request-1"
    assert "session-key" not in str(caught.value)
    http_client.close()
