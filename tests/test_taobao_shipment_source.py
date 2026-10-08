import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.workflows.shipment_watch_cli import run
from aftersales_workbench.workflows.taobao_shipment_source import (
    OFFICIAL_GATEWAY,
    TaobaoShipmentReadClient,
    TaobaoShipmentSource,
)


@pytest.fixture
def top():
    calls = []
    seller = {"user_id": 123, "nick": "synthetic-shop", "type": "C"}
    trade = {"tid": 456, "seller_nick": seller["nick"], "status": "WAIT_BUYER_CONFIRM_GOODS",
             "consign_time": "2026-10-08 08:00:00", "payment": "10.00", "orders": {"order": [
                 {"oid": 789, "refund_id": 999, "refund_status": "NO_REFUND"},
             ]}}
    refund = {"refund_id": 999, "tid": 456, "oid": 789, "status": "SUCCESS",
              "refund_fee": "10.00"}
    trace = {"tid": 456, "out_sid": "synthetic-tracking", "company_name": "中通快递",
             "trace_list": {"transit_step_info": [
                 {"status_time": "2026-10-08 09:00:00", "status_desc": "揽收"},
             ]}}
    responses = {
        "taobao.user.seller.get": {"user_seller_get_response": {"user": seller}},
        "taobao.trade.fullinfo.get": {"trade_fullinfo_get_response": {"trade": trade}},
        "taobao.refund.get": {"refund_get_response": {"refund": refund}},
        "taobao.logistics.orders.get": {"logistics_orders_get_response": {"shippings": {
            "shipping": [{"tid": 456, "out_sid": "synthetic-tracking",
                          "company_name": "中通快递", "status": "CREATED"}],
        }}},
        "taobao.logistics.trace.search": {"logistics_trace_search_response": trace},
    }

    def reply(request):
        assert str(request.url) == OFFICIAL_GATEWAY and request.method == "POST"
        params = dict(httpx.QueryParams(request.content.decode()))
        calls.append(params["method"])
        return httpx.Response(200, json=responses[params["method"]])

    config = SimpleNamespace(shop_code="taobao-test", platform_shop_id="123",
                             app_key=SecretStr("synthetic-app"),
                             app_secret=SecretStr("synthetic-secret"),
                             session_key=SecretStr("synthetic-main"))
    settings = Settings(_env_file=None, taobao_api_url=OFFICIAL_GATEWAY,
                        taobao_request_method="POST")
    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = TaobaoShipmentReadClient(config, settings, http_client=http)
        yield SimpleNamespace(client=client, config=config, settings=settings,
                              calls=calls, seller=seller, trade=trade, trace=trace,
                              responses=responses)


@pytest.mark.parametrize("gateway,verb", [
    ("https://relay.example/", "POST"),
    ("https://eco.taobao.com.attacker.example/router/rest", "POST"),
    (OFFICIAL_GATEWAY, "GET"),
])
def test_reject_nonofficial_transport_before_network(top, gateway, verb):
    top.settings.taobao_api_url, top.settings.taobao_request_method = gateway, verb
    with pytest.raises(ValueError, match="官方"):
        TaobaoShipmentReadClient(top.config, top.settings)
    assert top.calls == []


@pytest.mark.parametrize("method", ["taobao.rp.refunds.agree", "taobao.rp.refund.review",
                                    "taobao.top.auth.token.refresh"])
def test_write_methods_blocked_even_via_read_path(top, method):
    with pytest.raises(ValueError, match="白名单"):
        top.client.execute_read(method)
    with pytest.raises(ValueError, match="业务写入"):
        top.client.execute_write(method)
    with pytest.raises(ValueError, match="禁止退款"):
        top.client.agree_refund(refund_id=1)
    assert top.calls == []


@pytest.mark.parametrize("field,value", [("user_id", 321), ("type", "B"), ("nick", "")])
def test_identity_must_be_correct_c_seller(top, field, value):
    top.seller[field] = value
    with pytest.raises(ValueError, match="身份"):
        top.client.get_seller()
    assert top.client.seller_nick is None
    with pytest.raises(ValueError, match="先验证"):
        top.client.get_trade_fullinfo(tid=456)
    assert top.calls == ["taobao.user.seller.get"]


@pytest.mark.parametrize("field,value", [("tid", 123), ("seller_nick", "other")])
def test_each_order_cross_checks_seller(top, field, value):
    top.client.get_seller()
    top.trade[field] = value
    with pytest.raises(ValueError, match="卖家"):
        TaobaoShipmentSource(top.client).refresh("456")
    assert "taobao.logistics.orders.get" not in top.calls


def test_official_parcel_and_trace_preserve_taobao_identity(top):
    top.client.get_seller()
    source = TaobaoShipmentSource(top.client)
    assert source.platform == "TAOBAO" and source.window == timedelta(hours=20)
    parcel = source.refresh("456")[0]
    assert parcel.order_sn == "456" and parcel.shipped_at.hour == 0
    proof = source.trace_evidence(parcel, {})
    assert proof["source"] == "TAOBAO_TRACE" and proof["result"] == "HAS_TRACE"
    assert "status_desc" not in json.dumps(proof)


def test_full_refund_excluded_with_correct_platform(top):
    top.client.get_seller()
    top.trade["orders"]["order"][0]["refund_status"] = "SUCCESS"
    result = TaobaoShipmentSource(top.client).refresh("456")
    assert result.full_refund["platform"] == "TAOBAO" and not result
    assert "taobao.logistics.orders.get" not in top.calls


def test_transport_failure_never_retries(top):
    attempts = []
    def fail(request):
        attempts.append(1)
        raise httpx.ReadTimeout("synthetic-timeout")
    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        client = TaobaoShipmentReadClient(top.config, top.settings, http_client=http)
        with pytest.raises(RuntimeError):
            client.get_seller()
    assert len(attempts) == 1


@pytest.mark.parametrize("codes", [None, (), [], "taobao-test", [""], [1]])
def test_taobao_requires_explicit_shop_allowlist_before_database(top, codes):
    with pytest.raises(ValueError, match="白名单"):
        run(top.settings, platforms=("TAOBAO",), taobao_shop_codes=codes)
