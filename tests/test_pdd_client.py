from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.pdd.client import (
    PDD_REFUND_LIST_INCREMENT_GET,
    PddApiError,
    PddClient,
    PddCredentials,
    PddTransportError,
    generate_sign,
)


@pytest.fixture
def credentials() -> PddCredentials:
    return PddCredentials(
        shop_code="shop-1",
        client_id=SecretStr("client-id"),
        client_secret=SecretStr("testSecret"),
        access_token=SecretStr("access-token"),
    )


def test_seven_shop_defaults_use_two_application_groups() -> None:
    settings = Settings(_env_file=None)

    assert [
        settings.pdd_shop_1_app,
        settings.pdd_shop_2_app,
        settings.pdd_shop_3_app,
        settings.pdd_shop_4_app,
        settings.pdd_shop_5_app,
        settings.pdd_shop_6_app,
        settings.pdd_shop_7_app,
    ] == [1, 1, 1, 1, 2, 2, 2]


def test_generate_sign_sorts_parameters_and_returns_uppercase_md5() -> None:
    parameters = {"foo": "1", "bar": "2"}

    assert generate_sign(parameters, "testSecret") == "6DC171ED741204272E0CF9AEAF964E7E"


def test_build_signed_payload_does_not_expose_client_secret(
    credentials: PddCredentials,
) -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = PddClient(credentials, http_client=http_client, now=lambda: 1_700_000_000)

    payload = client.build_signed_payload("pdd.test", {"flag": True, "items": [1, 2]})

    assert payload["timestamp"] == "1700000000"
    assert payload["flag"] == "true"
    assert payload["items"] == "[1,2]"
    assert payload["sign"].isupper()
    assert "client_secret" not in payload
    assert "testSecret" not in json.dumps(payload)
    http_client.close()


def test_read_request_retries_temporary_gateway_error(credentials: PddCredentials) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"mall_info_get_response": {"mall_id": 1}}, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = PddClient(
        credentials,
        http_client=http_client,
        read_max_attempts=2,
        sleep=lambda _: None,
    )

    response = client.get_mall_info()

    assert attempts == 2
    assert response["mall_info_get_response"]["mall_id"] == 1
    http_client.close()


def test_api_error_keeps_error_code_and_request_id(credentials: PddCredentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error_response": {
                    "error_code": 10019,
                    "error_msg": "access denied",
                    "request_id": "request-1",
                }
            },
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = PddClient(credentials, http_client=http_client)

    with pytest.raises(PddApiError) as caught:
        client.get_mall_info()

    assert caught.value.error_code == 10019
    assert caught.value.request_id == "request-1"
    http_client.close()


def test_refund_query_rejects_a_window_longer_than_30_minutes(
    credentials: PddCredentials,
) -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = PddClient(credentials, http_client=http_client)

    with pytest.raises(ValueError, match="30 分钟"):
        client.get_refund_list_increment(start_updated_at=0, end_updated_at=1801)
    http_client.close()


def test_refund_query_sends_expected_api_type(credentials: PddCredentials) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200,
            json={"refund_increment_get_response": {"refund_list": []}},
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = PddClient(credentials, http_client=http_client)

    client.get_refund_list_increment(start_updated_at=100, end_updated_at=200)

    assert captured["type"] == PDD_REFUND_LIST_INCREMENT_GET
    assert captured["after_sales_status"] == "2"
    assert captured["after_sales_type"] == "1"
    http_client.close()


def test_transport_error_does_not_include_credentials(credentials: PddCredentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = PddClient(credentials, http_client=http_client, read_max_attempts=1)

    with pytest.raises(PddTransportError) as caught:
        client.get_mall_info()

    assert "access-token" not in str(caught.value)
    assert "testSecret" not in str(caught.value)
    http_client.close()


def test_non_retryable_http_error_is_attempted_only_once(credentials: PddCredentials) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = PddClient(
        credentials,
        http_client=http_client,
        read_max_attempts=3,
        sleep=lambda _: None,
    )

    with pytest.raises(PddTransportError, match="HTTP 401"):
        client.get_mall_info()

    assert attempts == 1
    http_client.close()
