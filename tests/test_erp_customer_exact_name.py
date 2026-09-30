"""ERP 名称是精确查询键，界面不易察觉的首尾空白也可能有业务含义。"""
import httpx
import pytest

from aftersales_workbench.integrations.erp.package_orders import ErpPackageOrderSource
from aftersales_workbench.integrations.erp.sales_owner import ErpWebSalesOwnerResolver
from tests.test_sales_owner_zero_quantity import SN, page, row


def client(name, *, profile_id="1", missing_link=False):
    queries = []

    def handler(request):
        path = request.url.path
        if path.endswith("loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("GetCustomerName"):
            return httpx.Response(200, json=[{"id": "1", "autocomplete": name + "@档案归属"}])
        if path.endswith("stdview"):
            actual = request.url.params["autocustomer"]
            queries.append(actual)
            return httpx.Response(200, text=(f"shipment?kehuid={profile_id}"
                if actual == name and not missing_link else "客户档案未找到"))
        if path.endswith("shipment"):
            assert request.url.params["kehuid"] == "1"
            return httpx.Response(200, text=page([row("4")]))
        return httpx.Response(200, text="login")

    return httpx.Client(base_url="https://erp.example", transport=httpx.MockTransport(handler)), queries


@pytest.mark.parametrize("name", ["\u3000测试客户", " 测试客户", "测试客户\u3000", "测试客户 ", "测试客户"])
def test_owner_queries_exact_returned_customer_name(name):
    http, queries = client(name)
    resolver = ErpWebSalesOwnerResolver(base_url="https://erp.example", username="test",
        password="test", http_client=http, cache_seconds=0)
    try:
        result = resolver.resolve(SN)
        assert result.status == "matched" and result.sales_owner == "原销售业务员"
        assert queries == [name]
    finally:
        resolver.close()


@pytest.mark.parametrize("name", ["\u3000", " ", ""])
def test_blank_customer_name_still_fails_before_profile_lookup(name):
    http, queries = client(name)
    source = ErpPackageOrderSource(base_url="https://erp.example", username="test",
        password="test", http_client=http)
    try:
        with pytest.raises(ValueError, match="身份字段不完整"):
            source.read(SN, only_order=True, for_owner_lookup=True)
        assert queries == []
    finally:
        source.close()


@pytest.mark.parametrize("owner_lookup", [True, False])
@pytest.mark.parametrize("profile_id,missing_link,message", [
    ("2", False, "客户 ID 不一致"), ("1", True, "未返回可核验的销售页链接"),
])
def test_preserving_name_does_not_bypass_customer_identity_check(owner_lookup, profile_id, missing_link, message):
    http, _ = client("\u3000测试客户", profile_id=profile_id, missing_link=missing_link)
    source = ErpPackageOrderSource(base_url="https://erp.example", username="test",
        password="test", http_client=http)
    try:
        with pytest.raises(ValueError, match=message):
            source.read(SN, only_order=True, for_owner_lookup=owner_lookup)
    finally:
        source.close()
