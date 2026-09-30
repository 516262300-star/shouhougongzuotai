from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from aftersales_workbench.integrations.erp.package_orders import ErpPackageOrderSource
from aftersales_workbench.integrations.erp.sales_owner import ErpWebSalesOwnerResolver, ErpSalesOwnerSyncService, SalesOwnerLookup
from aftersales_workbench.integrations.marketplace.douyin import DouyinReadClient
from aftersales_workbench.integrations.marketplace.douyin_payment import verified_order_payment
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from tests.test_douyin_onboarding import config, sample
from tests.test_erp_owner_retry import db, add_order, Resolver

EMPTY = '<script>var x = "上一页 1/0 下一页";</script>上一页 1/0 下一页<table></table>当前没有单据 上一页 1/0 下一页'


def erp_client(document, profile="shipment?kehuid=1"):
    def handler(request):
        path = request.url.path
        if path.endswith("loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("GetCustomerName"):
            return httpx.Response(200, json=[{"id": "1", "autocomplete": "测试客户@档案归属"}])
        return httpx.Response(200, text=(document if path.endswith("shipment") else profile))
    return httpx.Client(base_url="https://erp.example", transport=httpx.MockTransport(handler))


def resolve_empty(document=EMPTY, profile="shipment?kehuid=1"):
    resolver = ErpWebSalesOwnerResolver(base_url="https://erp.example", username="test",
        password="test", http_client=erp_client(document, profile), cache_seconds=0)
    try:
        return resolver.resolve("123")
    finally:
        resolver.close()


def test_explicit_empty_sales_page_is_not_a_query_failure():
    result = resolve_empty()
    assert result.status == "sales_not_found"
    assert result.customer_name == "测试客户" and result.sales_owner is None
    assert AftersalesRecordService._serialize_owner(result)["sales_owner"] == "ERP 暂无销售记录"


@pytest.mark.parametrize("document", [
    EMPTY.replace("当前没有单据", ""), EMPTY + "请重新登录",
    EMPTY + "<table><tr><td>RC-123</td></tr></table>",
    EMPTY.replace("1/0", "1/1"), EMPTY.replace("1/0", "2/0"),
    '<script>上一页 1/0 下一页 当前没有单据 上一页 1/0 下一页</script>',
])
def test_unproven_or_contradictory_empty_page_stays_unavailable(document):
    assert resolve_empty(document).status == "unavailable"


def test_empty_page_requires_matching_customer():
    assert resolve_empty(profile="shipment?kehuid=2").status == "unavailable"


@pytest.mark.parametrize("only_order", [True, False])
def test_empty_page_cannot_authorize_package_or_refund_checks(only_order):
    source = ErpPackageOrderSource(base_url="https://erp.example", username="test",
        password="test", http_client=erp_client(EMPTY))
    try:
        with pytest.raises(ValueError):
            source.read("123", only_order=only_order)
    finally:
        source.close()


def test_empty_sales_cache_is_neither_fast_refund_nor_failed_retry(db):
    order = add_order(db, 1, status="unavailable")
    result = ErpSalesOwnerSyncService(db, Resolver(SalesOwnerLookup(
        None, "测试客户", "sales_not_found", "暂无销售"))).sync_stale(limit=1, refresh_seconds=86400)
    assert (result.not_found, result.unavailable, result.not_required) == (1, 0, 0)
    assert order.erp_sales_owner_status == "sales_not_found"
    assert ErpSalesOwnerSyncService(db, Resolver()).sync_stale(limit=1, refresh_seconds=86400).scanned == 0
    assert "暂无销售" in AftersalesRecordService._cached_owner(order).message


def body(amount=2436, **kwargs):
    return {"data": {"shop_order_detail": dict(order_id="456", shop_id="123", pay_amount=amount, **kwargs)}}


@pytest.mark.parametrize("amount", [None, True, -1, "1.1", "NaN", "Infinity", "bad", 10000000000])
def test_invalid_payment_cannot_be_imported(amount):
    with pytest.raises(ValueError):
        verified_order_payment(body(amount), order_sn="456", shop_id="123")


@pytest.mark.parametrize("field", ["order_id", "shop_id"])
def test_payment_requires_exact_identity(field):
    value = body(); value["data"]["shop_order_detail"][field] = "999"
    with pytest.raises(ValueError, match="身份"):
        verified_order_payment(value, order_sn="456", shop_id="123")


def test_payment_is_parent_paid_amount_in_yuan_including_explicit_zero():
    assert verified_order_payment(body(sku_order_list=[{"pay_amount": 100}]), order_sn="456", shop_id="123") == Decimal("24.36")
    assert verified_order_payment(body(0), order_sn="456", shop_id="123") == Decimal("0.00")


def test_sync_reads_parent_payment_once_for_multiple_refunds(tmp_path):
    settings, shop = config(tmp_path)
    record, detail = sample()
    record["order_info"]["shop_order_id"] = detail["order_info"]["shop_order_id"] = "456"
    second = deepcopy(record); second["aftersale_info"]["aftersale_id"] = "a2"
    calls = []
    with DouyinReadClient(shop, settings) as client:
        client.execute_read = lambda *_: {"data": {"items": [record, second], "total": 2}}
        def get_detail(sn):
            result = deepcopy(detail); result["process_info"]["after_sale_info"]["after_sale_id"] = sn
            return {"data": result}
        client.get_detail = get_detail
        client.get_order_detail = lambda sn: calls.append(sn) or body()
        refunds = list(client.fetch_window(start_modified_at=1, end_modified_at=2, page_size=50))
        assert calls == ["456"] and len(refunds) == 2
        assert all(r.platform_order_amount == Decimal("24.36") for r in refunds)
        assert all(r.actual_refund_amount == Decimal("17.44") and r.refund_amount == Decimal("19.37") for r in refunds)
        client.get_order_detail = lambda _: {"data": {}}
        with pytest.raises(ValueError):
            list(client.fetch_window(start_modified_at=1, end_modified_at=2, page_size=50))

