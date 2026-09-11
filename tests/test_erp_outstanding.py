import re
from decimal import Decimal

import httpx
import pytest

from aftersales_workbench.integrations.erp.outstanding import outstanding_records
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedItem,
    ErpUnshippedRefundStatus,
    ErpWebUnshippedRefundClient,
)
from tests import test_erp_unshipped_refund as baseline

HEADERS = ["订单编号", "型号", "完整颜色", "欠货量"]
ROW = ["DD-TEST-001", "TEST-SKU-01", "测试色", "6"]


def empty_status():
    # 只保留真实页面的空状态结构，客户资料和订单不进入测试仓库。
    return ('<a href="#home" role="tab">状态表订单</a>'
            '<div role="tabpanel" class="tab-pane active" id="home">'
            '<style>.row-fluid { margin: 0; }</style>'
            '<div class="row-fluid"><div class="col-lg-12 col-sm-12">'
            '<div id="cart"><div id="heading"></div></div></div></div>'
            '<div id="heading"></div>'
            '<div class="col-lg-offset-9 alert-info ">总计： 0</div></div>')


def test_explicit_complete_empty_state_is_not_missing_data():
    assert outstanding_records(empty_status()) == []
    assert ErpWebUnshippedRefundClient._parse_outstanding_items(
        empty_status(), "DD-TEST-001") == ()


@pytest.mark.parametrize("case", [
    "total_missing", "positive_total", "bad_total", "title_missing", "cart_missing",
    "duplicate_section", "duplicate_total", "unknown_text", "table_present",
    "iframe_pending", "script_pending", "truncated_section", "truncated_page",
    "permission", "timeout", "auth_expired", "only_total", "other_tab",
])
def test_incomplete_or_failed_empty_state_is_held(case):
    page = empty_status()
    if case == "total_missing":
        page = page.replace("总计： 0", "")
    elif case == "positive_total":
        page = page.replace("总计： 0", "总计： 1")
    elif case == "bad_total":
        page = page.replace("总计： 0", "总计： 未知")
    elif case == "title_missing":
        page = page.replace("状态表订单", "其他页面")
    elif case == "cart_missing":
        page = page.replace('id="cart"', 'id="other"')
    elif case == "duplicate_section":
        page += empty_status()
    elif case == "duplicate_total":
        page = page.replace("总计： 0", '总计： 0<div class="alert-info">总计： 0</div>')
    elif case == "unknown_text":
        page = page.replace('id="heading">', 'id="heading">数据未加载')
    elif case == "table_present":
        page = page.replace('id="heading">', 'id="heading"><table></table>')
    elif case == "iframe_pending":
        page = page.replace('id="heading">', 'id="heading"><iframe></iframe>')
    elif case == "script_pending":
        page = page.replace('id="heading">', 'id="heading"><script>load()</script>')
    elif case == "truncated_section":
        page = page.removesuffix("</div>")
    elif case == "truncated_page":
        page = "<html><body>" + page
    elif case == "permission":
        page += "<div>权限不足</div>"
    elif case == "timeout":
        page += "<div>查询失败：请求超时</div>"
    elif case == "auth_expired":
        page += "<div>授权过期</div>"
    elif case == "only_total":
        page = "<div>总计： 0</div>"
    elif case == "other_tab":
        page = page.replace('id="home"', 'id="other"')
    with pytest.raises(ValueError):
        outstanding_records(page)


def test_tables_are_scoped_and_all_outstanding_tables_are_read():
    page = (baseline._table(HEADERS, []) + baseline._table(HEADERS, [ROW])
            + baseline._table(["客户名字", "累计应收"], [["测试客户", "0"]]))
    result = ErpWebUnshippedRefundClient._parse_outstanding_items(page, "DD-TEST-001")
    assert result == (ErpUnshippedItem("TEST-SKU-01", "测试色", Decimal("6")),)


@pytest.mark.parametrize("case", [
    "short_first_row", "short_later_row", "extra_cell", "partial_header", "duplicate_header",
    "duplicate_row", "empty_order", "empty_product", "colspan", "unclosed_table",
    "cell_outside_row", "loading_text", "unclosed_cell",
])
def test_malformed_rows_cannot_disappear_from_outstanding(case):
    headers, rows = HEADERS.copy(), [ROW.copy()]
    if case == "short_first_row":
        rows = [ROW[:-1]]
    elif case == "short_later_row":
        rows.append(ROW[:-1])
    elif case == "extra_cell":
        rows[0].append("unknown")
    elif case == "partial_header":
        headers[2] = "未知列"
    elif case == "duplicate_header":
        headers.append("欠货量")
        rows[0].append("6")
    elif case == "duplicate_row":
        rows.append(ROW.copy())
    elif case == "empty_order":
        rows[0][0] = ""
    elif case == "empty_product":
        rows[0][1] = ""
    page = baseline._table(headers, rows)
    if case == "colspan":
        page = page.replace("<td>", '<td colspan="2">', 1)
    elif case == "unclosed_table":
        page = page.removesuffix("</table>")
    elif case == "cell_outside_row":
        page = baseline._table(HEADERS, []).replace("</table>", "<td>6</td></table>")
    elif case == "loading_text":
        page = baseline._table(HEADERS, []).replace("</table>", "正在加载</table>")
    elif case == "unclosed_cell":
        page = page.replace("</td>", "", 1)
    with pytest.raises(ValueError):
        outstanding_records(page)


@pytest.mark.parametrize("case", [
    "valid", "no_refund_reference", "receivable_open", "query_timeout",
])
def test_empty_outstanding_alone_cannot_close_finances(case):
    client, state = baseline._client(initially_completed=True)
    original = client._get
    profile = baseline._profile(completed=True)
    profile = re.sub(r"<table>.*?</table>", lambda m: empty_status()
                     if "欠货量" in m[0] else m[0], profile, flags=re.S)
    if case == "no_refund_reference":
        profile = profile.replace("SK-TEST-1", "missing")
    elif case == "receivable_open":
        profile = profile.replace("0.00", "1.00")
    def get(path, *, params=None):
        if path.endswith("/stdview"):
            if case == "query_timeout":
                raise httpx.ReadTimeout("test timeout")
            return profile
        return original(path, params=params)
    client._get = get
    try:
        lookup = client.inspect(
            platform_order_sn=baseline.ORDER_SN, after_sales_sn=baseline.AFTER_SALES_SN,
            expected_amount=Decimal("74.51"), expected_items=[
                ErpUnshippedItem(baseline.SKU_CODE, baseline.SKU_COLOR, Decimal("6"))],
        )
        expected = (ErpUnshippedRefundStatus.COMPLETED if case == "valid"
                    else ErpUnshippedRefundStatus.UNAVAILABLE if case == "query_timeout"
                    else ErpUnshippedRefundStatus.NOT_FOUND)
        assert lookup.status is expected
        assert not state["write_called"]
    finally:
        client.close()
