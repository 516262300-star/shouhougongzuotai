from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.integrations.tmall.client import (
    TAOBAO_REFUND_GET,
    TAOBAO_REFUNDS_RECEIVE_GET,
    TAOBAO_RP_REFUND_REVIEW,
    TAOBAO_RP_REFUNDS_AGREE,
    TmallApiError,
    TmallClient,
    TmallConfigurationError,
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


def test_relay_mode_sends_signed_parameters_by_get(
    credentials: TmallCredentials,
) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"user_seller_get_response": {"user": {"user_id": 1, "nick": "店"}}},
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TmallClient(
        credentials,
        api_url="http://relay.example/forward.ashx",
        request_method="GET",
        http_client=http_client,
    )

    client.get_seller()

    assert captured["method"] == "taobao.user.seller.get"
    assert captured["app_key"] == "app-key"
    assert captured["session"] == "session-key"
    assert captured["sign"]
    assert "app_secret" not in captured
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


def test_write_request_is_blocked_by_default(credentials: TmallCredentials) -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = TmallClient(credentials, http_client=http_client)

    with pytest.raises(TmallConfigurationError, match="TMALL_WRITE_ENABLED"):
        client.execute_write(TAOBAO_RP_REFUND_REVIEW, refund_id=1)

    http_client.close()


def test_agree_refund_uses_main_review_then_child_agree(
    credentials: TmallCredentials,
) -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(httpx.QueryParams(request.content.decode()))
        requests.append(payload)
        if payload["method"] == TAOBAO_REFUND_GET:
            body = {
                "refund_get_response": {
                    "refund": {
                        "refund_id": "9001",
                        "status": "WAIT_SELLER_CONFIRM_GOODS",
                        "refund_fee": "26.42",
                        "refund_version": "10001",
                        "refund_phase": "onsale",
                    }
                }
            }
        elif payload["method"] == TAOBAO_RP_REFUND_REVIEW:
            body = {
                "rp_refund_review_response": {
                    "is_success": True,
                    "request_id": "review-request",
                }
            }
        else:
            body = {
                "rp_refunds_agree_response": {
                    "succ": True,
                    "request_id": "agree-request",
                }
            }
        return httpx.Response(200, json=body, request=request)

    child_credentials = TmallCredentials(
        shop_code="tmall-shop-01",
        app_key=credentials.app_key,
        app_secret=credentials.app_secret,
        session_key=SecretStr("refund-child-session"),
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    client = TmallClient(
        credentials,
        write_enabled=True,
        http_client=http_client,
        sleep=sleeps.append,
    )

    result = client.agree_refund(
        refund_id=9001,
        refund_credentials=child_credentials,
    )

    assert [request["method"] for request in requests] == [
        TAOBAO_REFUND_GET,
        TAOBAO_RP_REFUND_REVIEW,
        TAOBAO_RP_REFUNDS_AGREE,
    ]
    assert requests[1]["session"] == "session-key"
    assert requests[2]["session"] == "refund-child-session"
    assert requests[2]["refund_infos"] == "9001|2642|10001|onsale"
    assert requests[2]["ignore_code"] == "true"
    assert sleeps == [1.0]
    assert result["already_refunded"] is False
    assert result["agree_request_id"] == "agree-request"
    http_client.close()


def test_agree_refund_blocks_amount_over_limit_before_any_write(
    credentials: TmallCredentials,
) -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(httpx.QueryParams(request.content.decode()))
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "refund_get_response": {
                    "refund": {
                        "refund_id": "9002",
                        "status": "WAIT_SELLER_CONFIRM_GOODS",
                        "refund_fee": "30.01",
                        "refund_version": "10002",
                        "refund_phase": "onsale",
                    }
                }
            },
            request=request,
        )

    child_credentials = TmallCredentials(
        shop_code="tmall-shop-01",
        app_key=credentials.app_key,
        app_secret=credentials.app_secret,
        session_key=SecretStr("refund-child-session"),
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TmallClient(
        credentials,
        write_enabled=True,
        http_client=http_client,
    )

    with pytest.raises(TmallConfigurationError, match="超过自动退款上限"):
        client.agree_refund(
            refund_id=9002,
            refund_credentials=child_credentials,
            max_refund_amount=Decimal("30.00"),
        )

    assert [request["method"] for request in requests] == [TAOBAO_REFUND_GET]
    http_client.close()
