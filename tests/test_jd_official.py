from __future__ import annotations

import socket
import traceback
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.integrations.marketplace.jd_official import (
    JdOfficialApiError,
    JdOfficialCredentials,
    JdOfficialProtocolError,
    JdOfficialReadClient,
    generate_jd_sp_sign,
)
from aftersales_workbench.integrations.marketplace.models import MarketplaceTransportError

START = 1_700_000_000_000
END = START + 60_000
CREDENTIALS = JdOfficialCredentials(
    vender_id="9001",
    app_key=SecretStr("offline-app"),
    app_secret=SecretStr("offline-secret"),
    access_token=SecretStr("offline-token"),
)


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("京东官方离线测试禁止真实网络")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def client_for():
    clients = []

    def make(handler):
        client = JdOfficialReadClient(
            CREDENTIALS, transport=httpx.MockTransport(handler), now_ms=lambda: START
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def row(afs_id="1001", order_id="2001", vender_id="9001"):
    return {
        "afsOrderId": afs_id,
        "orderId": order_id,
        "afsOrderBaseInfo": {"buId": vender_id},
        "orderInfo": {"orderId": order_id},
    }


def page_body(rows=None, *, page=1, size=2, total=None):
    rows = [row()] if rows is None else rows
    return {
        "Response": {
            "success": True,
            "data": rows,
            "paginationData": {
                "currentPage": page,
                "pageSize": size,
                "totalItems": len(rows) if total is None else total,
            },
        }
    }


def detail_body():
    return {
        "Response": {
            "success": True,
            "data": {
                "afsOrderId": "1001",
                "afsOrderBaseInfo": {"applyTime": START, "modifiedTime": END},
                "orderInfo": {"orderId": "2001", "orderWarehouseStatus": 1},
                "relationInfo": {"orderId": "2001", "srcOrderId": "2999"},
                "afsOrderStatusInfo": {"mainStatus": 100, "subStatus": 1000},
                "customerApplyInfo": {"customerExpect": 10},
                "refundInfo": {
                    "refundStatus": 20,
                    "applyRefundAmount": "12.34",
                    "actualRefundAmount": "10.00",
                    "estimateRefundDetail": {"maxRefundAmount": "12.00"},
                },
                "skuInfoList": [
                    {
                        "skuId": "3001",
                        "skuNum": 2,
                        "skuType": 10,
                        "skuUuid": "sku-uuid-1",
                        "skuName": "模拟商品 蓝色",
                        "partCode": "PART-1",
                    },
                    {"skuId": "3002", "skuNum": 1, "skuType": 20},
                ],
                "waybillInfoList": [
                    {"waybillType": 2, "waybillCode": "RETURN-1", "providerId": 1},
                    {"waybillType": 2, "waybillCode": "RETURN-2", "providerId": 1},
                    {"waybillType": 3, "waybillCode": "REDELIVER-1", "providerId": 2},
                ],
                "customerInfo": {"customerName": "不应保留的姓名", "customerTel": "不应保留的电话"},
            },
        }
    }


def load_detail(client_for, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page_body() if len(calls) == 1 else body)

    client = client_for(handler)
    client.list_aftersales(start_modified_ms=START, end_modified_ms=END, page_size=2)
    return client.get_aftersale("1001")


def test_sign_fixed_vector_and_raw_unicode_values():
    assert generate_jd_sp_sign({"b": "2", "a": "1"}, "secret") == (
        "EF16F26C937CF52AE6F85DF2FD08B24A"
    )
    assert generate_jd_sp_sign({"a": "中文 &,"}, "secret") != generate_jd_sp_sign(
        {"a": "%E4%B8%AD%E6%96%87%20%26%2C"}, "secret"
    )
    with pytest.raises(ValueError):
        generate_jd_sp_sign({"a": 1}, "secret")


def test_fixed_official_get_headers_query_and_detail_signature(client_for):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url).startswith("https://api-cn.jd.com/rest/sp-aftercare/v0/afs-orders")
        assert request.headers["X-JOS-Request-Identity"] == "vender"
        assert request.headers["X-JOS-Access-Token"] == "offline-token"
        assert request.headers["X-JOS-Timestamp"] == str(START)
        assert not request.content
        assert "offline-secret" not in str(request.headers)
        assert "offline-token" not in str(request.url)
        if len(calls) == 1:
            assert dict(request.url.params) == {
                "updateStartTime": str(START),
                "updateEndTime": str(END),
                "page": "1",
                "pageSize": "2",
            }
            assert request.headers["X-JOS-Sign"] == "0627BD69EEC951A95E84A3A65FEB0AA2"
            return httpx.Response(200, json=page_body())
        assert request.url.path.endswith("/1001")
        assert dict(request.url.params) == {"scopeSet": "refundInfo,skuExtInfo"}
        assert request.headers["X-JOS-Sign"] == "62B85BF98B8A1C9D3C68A36761E2A5BD"
        return httpx.Response(200, json=detail_body())

    client = client_for(handler)
    result = client.read_window(start_modified_ms=START, end_modified_ms=END, page_size=2)
    assert len(result) == 1
    assert result[0].reference.vender_id == "9001"
    assert len(calls) == 2
    # 完整窗口读取后不留下详情查询权限缓存。
    with pytest.raises(ValueError, match="先在同一客户端"):
        client.get_aftersale("1001")


def test_unknown_detail_id_is_rejected_before_request(client_for):
    client = client_for(lambda _: pytest.fail("不应发请求"))
    with pytest.raises(ValueError, match="先在同一客户端"):
        client.get_aftersale("1002")
    for value in ("../1001", "1001?write=true", True, 1.1, "0001", ""):
        with pytest.raises(JdOfficialProtocolError):
            client.get_aftersale(value)


@pytest.mark.parametrize("value", [None, "other-shop", "9002", True])
def test_list_missing_or_foreign_vender_is_rejected(client_for, value):
    client = client_for(lambda _: httpx.Response(200, json=page_body([row(vender_id=value)])))
    with pytest.raises(JdOfficialProtocolError):
        client.list_aftersales(start_modified_ms=START, end_modified_ms=END, page_size=2)
    with pytest.raises(ValueError, match="先在同一客户端"):
        client.get_aftersale("1001")


def test_invalid_second_row_does_not_authorize_first_row(client_for):
    body = page_body([row(), row("1002", "2002", "9002")])
    client = client_for(lambda _: httpx.Response(200, json=body))
    with pytest.raises(JdOfficialProtocolError):
        client.list_aftersales(start_modified_ms=START, end_modified_ms=END, page_size=2)
    with pytest.raises(ValueError):
        client.get_aftersale("1001")


@pytest.mark.parametrize(
    "location,value",
    [
        ("afsOrderId", "1002"),
        ("orderInfo", {"orderId": "2002"}),
        ("relationInfo", {"orderId": "2002"}),
        ("afsOrderBaseInfo", {"buId": "9002"}),
        ("orderInfo", None),
    ],
)
def test_detail_conflicting_identity_is_rejected(client_for, location, value):
    body = detail_body()
    body["Response"]["data"][location] = value
    if location == "orderInfo" and value is None:
        body["Response"]["data"].pop("relationInfo")
    with pytest.raises(JdOfficialProtocolError):
        load_detail(client_for, body)


def test_detail_retains_all_skus_parcels_and_separates_money(client_for):
    result = load_detail(client_for, detail_body())
    assert result.apply_refund_amount == Decimal("12.34")
    assert result.estimated_max_refund_amount == Decimal("12.00")
    assert result.reported_actual_refund_amount == Decimal("10.00")
    assert result.confirmed_actual_refund_amount == Decimal("10.00")
    assert [item.sku_id for item in result.items] == ["3001", "3002"]
    assert [item.quantity for item in result.items] == [2, 1]
    assert result.items[0].part_code == "PART-1"  # 不能当成 ERP SKU。
    assert [item.tracking_number for item in result.waybills] == [
        "RETURN-1",
        "RETURN-2",
        "REDELIVER-1",
    ]
    assert [item.waybill_type for item in result.waybills] == [2, 2, 3]
    assert result.applied_at_ms == START
    assert result.modified_at_ms == END
    assert "不应保留" not in repr(result)


@pytest.mark.parametrize("status", [None, 10, 30, 999])
def test_service_complete_does_not_prove_refund_success(client_for, status):
    body = detail_body()
    body["Response"]["data"]["refundInfo"]["refundStatus"] = status
    result = load_detail(client_for, body)
    assert result.main_status == 100
    assert result.reported_actual_refund_amount == Decimal("10.00")
    assert result.confirmed_actual_refund_amount is None


def test_missing_money_is_not_replaced_by_estimate_or_application(client_for):
    body = detail_body()
    body["Response"]["data"]["refundInfo"] = {
        "refundStatus": 20,
        "estimateRefundDetail": {"maxRefundAmount": "12.00"},
    }
    result = load_detail(client_for, body)
    assert result.apply_refund_amount is None
    assert result.reported_actual_refund_amount is None
    assert result.confirmed_actual_refund_amount is None
    assert result.estimated_max_refund_amount == Decimal("12.00")


def test_nested_money_and_zero_are_not_lost(client_for):
    body = detail_body()
    body["Response"]["data"]["refundInfo"] = {
        "refundStatus": "20",
        "applyRefundDetail": {"refundAmount": "0.00"},
        "actualRefundDetail": {"actualRefundAmount": "0.00"},
    }
    result = load_detail(client_for, body)
    assert result.apply_refund_amount == Decimal("0.00")
    assert result.confirmed_actual_refund_amount == Decimal("0.00")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.01", True, "invalid", {}])
def test_invalid_money_fails_closed(client_for, value):
    body = detail_body()
    body["Response"]["data"]["refundInfo"]["actualRefundAmount"] = value
    with pytest.raises(JdOfficialProtocolError):
        load_detail(client_for, body)


@pytest.mark.parametrize(
    "kind,top,nested",
    [
        ("actualRefundDetail", "actualRefundAmount", "actualRefundAmount"),
        ("applyRefundDetail", "applyRefundAmount", "refundAmount"),
    ],
)
def test_conflicting_money_fails_closed(client_for, kind, top, nested):
    body = detail_body()
    refund = body["Response"]["data"]["refundInfo"]
    refund[kind] = {nested: "0.00"}
    assert refund[top] != "0.00"
    with pytest.raises(JdOfficialProtocolError, match="金额冲突"):
        load_detail(client_for, body)


def test_unknown_shipping_and_customer_expect_remain_unknown(client_for):
    body = detail_body()
    body["Response"]["data"]["orderInfo"].pop("orderWarehouseStatus")
    body["Response"]["data"]["customerApplyInfo"] = {"customerExpect": 999}
    result = load_detail(client_for, body)
    assert result.warehouse_status is None
    assert result.customer_expect == 999


@pytest.mark.parametrize(
    "field,value",
    [
        ("skuInfoList", {}),
        ("waybillInfoList", {}),
        ("skuInfoList", [{"skuId": "3001", "skuNum": 0}]),
        ("skuInfoList", [{"skuId": "3001", "skuNum": 1.5}]),
        ("waybillInfoList", [{"waybillType": 2}]),
    ],
)
def test_malformed_item_or_parcel_is_not_silently_discarded(client_for, field, value):
    body = detail_body()
    body["Response"]["data"][field] = value
    with pytest.raises(JdOfficialProtocolError):
        load_detail(client_for, body)


@pytest.mark.parametrize("wrapped,success", [(True, True), (False, True), (False, "true")])
def test_explicit_empty_page_and_documented_envelopes(client_for, wrapped, success):
    body = page_body([])
    body["Response"]["success"] = success
    if not wrapped:
        body = body["Response"]
    client = client_for(lambda _: httpx.Response(200, json=body))
    assert client.read_window(start_modified_ms=START, end_modified_ms=END, page_size=2) == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("data", None),
        ("data", {}),
        ("data", [None]),
        ("paginationData", None),
        ("paginationData", {"totalItems": 0}),
        ("paginationData", {"totalItems": 0, "currentPage": 2, "pageSize": 2}),
        ("paginationData", {"totalItems": 0, "currentPage": 1, "pageSize": 3}),
        ("paginationData", {"totalItems": True, "currentPage": 1, "pageSize": 2}),
        ("paginationData", {"totalItems": 1, "currentPage": 1, "pageSize": 2}),
    ],
)
def test_incomplete_page_cannot_masquerade_as_empty(client_for, field, value):
    body = page_body([])
    body["Response"][field] = value
    client = client_for(lambda _: httpx.Response(200, json=body))
    with pytest.raises(JdOfficialProtocolError):
        client.read_window(start_modified_ms=START, end_modified_ms=END, page_size=2)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Response": None},
        {"Response": {"success": True}, "success": True},
        {"success": 1},
        {"success": "false"},
        {"success": False},
    ],
)
def test_http_success_is_not_business_success(client_for, body):
    client = client_for(lambda _: httpx.Response(200, json=body))
    with pytest.raises((JdOfficialProtocolError, JdOfficialApiError)):
        client.list_aftersales(start_modified_ms=START, end_modified_ms=END, page_size=2)


def test_two_pages_then_details_complete_atomically(client_for):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/afs-orders"):
            page = int(request.url.params["page"])
            rows = [row(), row("1002", "2002")] if page == 1 else [row("1003", "2003")]
            return httpx.Response(200, json=page_body(rows, page=page, total=3))
        afs_id = request.url.path.rsplit("/", 1)[1]
        body = detail_body()
        data = body["Response"]["data"]
        data["afsOrderId"] = afs_id
        data["orderInfo"]["orderId"] = str(int(afs_id) + 1000)
        data.pop("relationInfo")
        return httpx.Response(200, json=body)

    result = client_for(handler).read_window(
        start_modified_ms=START, end_modified_ms=END, page_size=2
    )
    assert [item.reference.afs_order_id for item in result] == ["1001", "1002", "1003"]
    assert len(calls) == 5
    assert calls[0] == calls[1]  # 完整验证列表后再补详情。


@pytest.mark.parametrize("case", ["duplicate_page", "changed_total", "short_page", "budget"])
def test_pagination_failures_never_start_details(client_for, case):
    calls = []

    def handler(request):
        assert request.url.path.endswith("/afs-orders")
        calls.append(request)
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json=page_body([row()], page=1, size=1, total=2))
        rows = [row()] if case == "duplicate_page" else [row("1002", "2002")]
        total = 3 if case == "changed_total" else 2
        if case == "short_page":
            rows = []
        return httpx.Response(200, json=page_body(rows, page=2, size=1, total=total))

    client = client_for(handler)
    with pytest.raises(JdOfficialProtocolError):
        client.read_window(
            start_modified_ms=START,
            end_modified_ms=END,
            page_size=1,
            max_pages=1 if case == "budget" else 10,
        )
    assert len(calls) == (1 if case == "budget" else 2)
    with pytest.raises(ValueError):
        client.get_aftersale("1001")


def test_duplicate_rows_fail(client_for):
    client = client_for(lambda _: httpx.Response(200, json=page_body([row(), row()])))
    with pytest.raises(JdOfficialProtocolError, match="重复售后"):
        client.read_window(start_modified_ms=START, end_modified_ms=END, page_size=2)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_error_codes_are_safe_and_never_retried(client_for, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "success": False,
                "errorList": [
                    {
                        "code": "99904030005",
                        "message": "offline-token",
                        "details": "offline-secret",
                    }
                ],
            },
        )

    client = client_for(handler)
    with pytest.raises(JdOfficialApiError) as caught:
        client.list_aftersales(start_modified_ms=START, end_modified_ms=END, page_size=2)
    assert caught.value.requires_yunding
    assert caught.value.codes == ("99904030005",)
    assert caught.value.http_status == status
    assert "offline-token" not in str(caught.value)
    assert "offline-secret" not in str(caught.value)
    assert len(calls) == 1


def test_success_with_error_list_is_rejected(client_for):
    body = page_body([])
    body["Response"]["errorList"] = {"code": "123", "details": "private-data"}
    client = client_for(lambda _: httpx.Response(200, json=body))
    with pytest.raises(JdOfficialApiError):
        client.list_aftersales(start_modified_ms=START, end_modified_ms=END, page_size=2)


@pytest.mark.parametrize("case", ["redirect", "html", "timeout", "network"])
def test_transport_failures_do_not_redirect_retry_or_leak(client_for, case):
    calls = []

    def handler(request):
        calls.append(request)
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "https://untrusted.example/"})
        if case == "html":
            return httpx.Response(200, text="<html>offline-token</html>")
        if case == "timeout":
            raise httpx.ReadTimeout("offline-token", request=request)
        raise httpx.ConnectError("offline-secret", request=request)

    client = client_for(handler)
    with pytest.raises(MarketplaceTransportError) as caught:
        client.list_aftersales(start_modified_ms=START, end_modified_ms=END, page_size=2)
    display = "".join(traceback.format_exception(caught.value))
    assert "offline-token" not in display
    assert "offline-secret" not in display
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_modified_ms": START // 1000},
        {"end_modified_ms": START},
        {"end_modified_ms": START - 1},
        {"end_modified_ms": START + 86_400_001},
        {"page": 0},
        {"page": True},
        {"page_size": 0},
        {"page_size": 51},
        {"start_modified_ms": True},
    ],
)
def test_bad_inputs_fail_before_request(client_for, kwargs):
    client = client_for(lambda _: pytest.fail("无效参数不应发请求"))
    arguments = {"start_modified_ms": START, "end_modified_ms": END, "page_size": 2}
    arguments.update(kwargs)
    with pytest.raises((ValueError, JdOfficialProtocolError)):
        client.list_aftersales(**arguments)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"app_secret": SecretStr("")},
        {"access_token": SecretStr("a\r\nb")},
        {"app_key": "plaintext"},
        {"vender_id": "jdx"},
        {"vender_id": "p2-"},
        {"vender_id": ""},
        {"vender_id": 9001},
    ],
)
def test_credentials_are_explicit_and_never_relay_labels(kwargs):
    with pytest.raises((ValueError, JdOfficialProtocolError)):
        replace(CREDENTIALS, **kwargs)
    assert "offline-" not in repr(CREDENTIALS)


def test_no_write_or_production_sync_entrypoint():
    assert not hasattr(JdOfficialReadClient, "execute_read")
    assert not hasattr(JdOfficialReadClient, "execute_write")
    assert not hasattr(JdOfficialReadClient, "refund")
    assert not hasattr(JdOfficialReadClient, "fetch_window")


def test_existing_production_factory_keeps_legacy_jd_client():
    from aftersales_workbench.db.models import Platform
    from aftersales_workbench.integrations.marketplace.jd import JdReadClient
    from aftersales_workbench.integrations.marketplace.runner import _CLIENT_TYPE

    assert _CLIENT_TYPE[Platform.JD] is JdReadClient


def test_failure_in_second_detail_returns_no_window_and_clears_scope(client_for):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/afs-orders"):
            return httpx.Response(200, json=page_body([row(), row("1002", "2002")]))
        if request.url.path.endswith("/1001"):
            return httpx.Response(200, json=detail_body())
        return httpx.Response(503, json={"success": False})

    client = client_for(handler)
    with pytest.raises(JdOfficialApiError):
        client.read_window(start_modified_ms=START, end_modified_ms=END, page_size=2)
    assert len(calls) == 3
    with pytest.raises(ValueError):
        client.get_aftersale("1001")


def test_request_ignores_environment_proxy(monkeypatch):
    # 即使部署环境有代理变量，客户端也不创建环境代理连接池。
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid:8443")
    monkeypatch.setattr(
        httpx._client, "get_environment_proxies", lambda: pytest.fail("不能读取环境代理")
    )
    with JdOfficialReadClient(CREDENTIALS) as client:
        assert client._client.follow_redirects is False
        assert client._client.trust_env is False


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1])
def test_bad_timeout_is_rejected(value):
    with pytest.raises(ValueError):
        JdOfficialReadClient(CREDENTIALS, timeout_seconds=value)


@pytest.mark.parametrize("value", [0, 101, True])
def test_bad_pagination_budget_is_rejected(client_for, value):
    client = client_for(lambda _: pytest.fail("预算无效时不应发请求"))
    with pytest.raises((ValueError, JdOfficialProtocolError)):
        client.read_window(start_modified_ms=START, end_modified_ms=END, max_pages=value)
