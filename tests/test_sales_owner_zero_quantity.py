"""零数量历史商品行不能阻止归属查询，也不能授予包裹数量核验资格。"""
import httpx
import pytest

from aftersales_workbench.integrations.erp.package_orders import ErpPackageOrderSource
from aftersales_workbench.integrations.erp.sales_owner import ErpWebSalesOwnerResolver

SN = "3300000000000000001"
HEADERS = ["编号", "完成日期", "型号", "颜色", "订单编号", "客户编号", "入库化只", "归属业务员"]


def page(rows):
    return "上一页 1 / 1 下一页<table>" + "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in [HEADERS, *rows]
    ) + "</table>"


def row(quantity, *, owner="原销售业务员", sale_id="101"):
    return ["RC-SYNTHETIC", "2026-09-01", "示例型号", "银色", sale_id, SN, quantity, owner]


def client(rows):
    def handler(request):
        path = request.url.path
        if path.endswith("loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("loginpage"):
            return httpx.Response(200, text="login")
        if path.endswith("GetCustomerName"):
            return httpx.Response(200, json=[{"id": "1", "autocomplete": "测试客户@档案业务员"}])
        if path.endswith("stdview"):
            return httpx.Response(200, text="shipment?kehuid=1")
        if path.endswith("shipment"):
            return httpx.Response(200, text=page(rows))
        raise AssertionError(path)
    return httpx.Client(base_url="https://erp.example", transport=httpx.MockTransport(handler))


def resolve(rows):
    resolver = ErpWebSalesOwnerResolver(base_url="https://erp.example", username="test",
        password="test", http_client=client(rows), cache_seconds=0)
    try:
        return resolver.resolve(SN)
    finally:
        resolver.close()


def test_owner_resolves_positive_sales_with_zero_quantity_history():
    result = resolve([row("6"), row("0")])
    assert result.status == "matched"
    assert result.sales_owner == "原销售业务员"


@pytest.mark.parametrize("quantity", ["-1", "NaN", "Infinity", "bad", ""])
def test_invalid_quantity_is_still_unavailable(quantity):
    assert resolve([row("6"), row(quantity)]).status == "unavailable"


def test_zero_only_keeps_original_sales_owner_without_proving_quantity():
    result = resolve([row("0")])
    assert result.status == "matched"
    assert result.sales_owner == "原销售业务员"
    assert "不代表数量或退款核验通过" in result.message
    source = ErpPackageOrderSource(base_url="https://erp.example", username="test",
        password="test", http_client=client([row("0")]))
    try:
        sales = source.read(SN, only_order=True, for_owner_lookup=True)
        assert sales.rows[0]["quantity"] == "0"
        assert sales.rows[0]["sale_id"] == "101"
    finally:
        source.close()


@pytest.mark.parametrize("owners,status", [
    (["原销售业务员", "其他业务员"], "conflict"),
    (["原销售业务员", ""], "not_found"),
    ([""], "not_found"),
])
def test_zero_only_still_requires_complete_unique_owner(owners, status):
    assert resolve([row("0", owner=owner) for owner in owners]).status == status


def test_zero_quantity_owner_conflict_is_not_ignored():
    assert resolve([row("6"), row("0", owner="其他业务员")]).status == "conflict"


def test_zero_quantity_missing_owner_is_not_ignored():
    assert resolve([row("6"), row("0", owner="")]).status == "not_found"


def test_zero_quantity_bad_sale_identity_is_not_ignored():
    assert resolve([row("6"), row("0", sale_id="bad")]).status == "unavailable"


def test_owner_accepts_sales_reference_suffix_without_stripping_identity():
    source = ErpPackageOrderSource(base_url="https://erp.example",username="test",password="test",
                                  http_client=client([row("6",sale_id="101Y")]))
    try:
        result=source.read(SN,only_order=True,for_owner_lookup=True)
        assert result.sales_owner=='原销售业务员' and result.rows[0]['sale_id']=='101Y'
    finally:source.close()


@pytest.mark.parametrize('sale_id',['','Y101','101/102','101Y 102'])
def test_owner_still_rejects_invalid_sales_reference(sale_id):
    assert resolve([row('6',sale_id=sale_id)]).status=='unavailable'


@pytest.mark.parametrize('only_order',[False,True])
def test_sales_reference_suffix_does_not_relax_package_or_funds_checks(only_order):
    source=ErpPackageOrderSource(platform='TMALL',base_url='https://erp.example',username='test',
                                password='test',http_client=client([row('6',sale_id='101Y')]))
    try:
        with pytest.raises(ValueError,match='原销售关联或数量无效'):
            source.read(SN,only_order=only_order)
    finally:source.close()


@pytest.mark.parametrize("only_order", [True, False])
@pytest.mark.parametrize("quantities", [["6", "0"], ["0"]])
def test_package_and_refund_quantity_validation_stays_strict(only_order, quantities):
    source = ErpPackageOrderSource(platform="TMALL", base_url="https://erp.example",
        username="test", password="test", http_client=client([row(q) for q in quantities]))
    with pytest.raises(ValueError, match="原销售关联或数量无效"):
        source.read(SN, only_order=only_order)
    source.close()


def test_owner_mode_requires_exact_order_filter():
    source = ErpPackageOrderSource(base_url="https://erp.example", username="test",
        password="test", http_client=client([row("6")]))
    with pytest.raises(ValueError, match="必须限定目标订单"):
        source.read(SN, for_owner_lookup=True)
    source.close()
