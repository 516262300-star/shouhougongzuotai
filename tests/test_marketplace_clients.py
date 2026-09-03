from __future__ import annotations

import json
from pathlib import Path

import httpx
from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import Platform
from aftersales_workbench.integrations.marketplace.douyin import DouyinReadClient
from aftersales_workbench.integrations.marketplace.jd import (
    JD_AFTERSALE_LIST,
    JD_ORDER_GET,
    JdReadClient,
)
from aftersales_workbench.integrations.marketplace.models import ConfiguredMarketplaceShop


def _shop(platform: Platform, **overrides: object) -> ConfiguredMarketplaceShop:
    values: dict[str, object] = {
        "platform": platform,
        "shop_number": 1,
        "shop_code": f"{platform.value.lower()}-01",
        "shop_name": "测试店",
        "platform_shop_id": "123456",
        "app_key": SecretStr("app-key"),
        "app_secret": SecretStr("app-secret"),
        "access_token": SecretStr("access-token"),
    }
    values.update(overrides)
    return ConfiguredMarketplaceShop(**values)  # type: ignore[arg-type]


def test_jd_relay_uses_get_query() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"ok": True}, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        jd_api_url="http://relay.example/forward.ashx",
        jd_request_method="GET",
    )
    client = JdReadClient(_shop(Platform.JD), settings, http_client=http_client)

    client.execute_read(JD_ORDER_GET, {"order_id": "O-1"})

    assert captured["method"] == JD_ORDER_GET
    assert json.loads(captured["360buy_param_json"])["order_id"] == "O-1"
    assert captured["sign"]
    assert "app_secret" not in captured
    http_client.close()


def test_jd_fetch_window_skips_service_bill_without_refund(monkeypatch) -> None:
    client = object.__new__(JdReadClient)

    def execute_read(method: str, _parameters: dict[str, object]) -> dict[str, object]:
        if method == JD_AFTERSALE_LIST:
            return {
                "jingdong_asc_serviceAndRefund_view_responce": {
                    "pageResult": {
                        "data": [
                            {
                                "sameOrderServiceBill": {
                                    "serviceId": "SERVICE-ONLY",
                                    "orderId": "ORDER-1",
                                }
                            }
                        ]
                    }
                }
            }
        return {
            "jingdong_pop_afs_soa_refundapply_queryPageList_responce": {
                "queryResult": {"result": []}
            }
        }

    monkeypatch.setattr(client, "execute_read", execute_read)
    monkeypatch.setattr(
        client,
        "get_order",
        lambda _order_id: (_ for _ in ()).throw(AssertionError("不应查询订单")),
    )

    records = list(
        client.fetch_window(
            start_modified_at=1_788_230_400,
            end_modified_at=1_788_234_000,
            page_size=50,
        )
    )

    assert records == []


def test_douyin_self_authorization_is_cached(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token/create":
            params = json.loads(request.content)
            assert params["grant_type"] == "authorization_self"
            assert params["shop_id"] == 123456
            return httpx.Response(
                200,
                json={
                    "code": 10000,
                    "data": {"access_token": "generated-token", "expires_in": 604800},
                },
                request=request,
            )
        return httpx.Response(200, json={"code": 10000, "data": {}}, request=request)

    cache_path = tmp_path / "douyin-token-cache.json"
    settings = Settings(
        _env_file=None,
        douyin_token_cache_path=str(cache_path),
    )
    shop = _shop(
        Platform.DOUYIN,
        access_token=None,
        access_token_mode="authorization_self",
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = DouyinReadClient(
        shop,
        settings,
        now=lambda: 1_700_000_000,
        http_client=http_client,
    )

    client.execute_read("/afterSale/List", {"page": 0})
    client.execute_read("/afterSale/List", {"page": 1})

    assert [request.url.path for request in requests] == [
        "/token/create",
        "/afterSale/List",
        "/afterSale/List",
    ]
    assert requests[1].url.params["access_token"] == "generated-token"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache[shop.shop_code]["access_token"] == "generated-token"
    http_client.close()
