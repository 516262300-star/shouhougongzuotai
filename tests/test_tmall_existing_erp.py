"""现有ERP管理页面适配：全部合成数据，不调用真实资金接口。"""

from html import escape
from unittest.mock import Mock

import pytest

from aftersales_workbench.integrations.erp.tmall_unshipped import read_existing_refund
from tests.test_tmall_module3 import OID, case, db, table  # noqa: F401


def detail_page(source):
    mapping = {"platform": "平台", "orderId": "平台单号", "refundId": "退款单号",
               "overall_status": "状态", "applyPayment": "退款金额", "applyCarriage": "退款运费",
               "detail": "Detail", "ddnr": "系统订单号", "csname": "系统客户名称",
               "isRefundGoods": "是否退货", "waybill": "运单号", "log": "操作记录"}
    return '<html><body><div class="panel panel-bordered">' + ''.join(
        '<div class="panel-heading"><h3>' + label + '</h3></div>'
        '<div class="panel-body">' + escape(str(source[key])) + '</div>'
        for key, label in mapping.items() if key in source
    ) + '</div></body></html>'


@pytest.fixture
def existing(case):  # noqa: F811 -- pytest 注入本模块导入的合成数据夹具
    case.cfg.tmall_module3_erp_read_mode = "existing_admin"
    original_get = case.erp._get.side_effect
    original_response = case.erp._get_response.side_effect

    def get(path, *, params):
        if path.endswith('/admin/refunds/77'):
            return detail_page(case.source)
        return original_get(path, params=params)

    def response(path, *, params):
        assert '/module3-inspect' not in path and 'showlist' not in path
        return original_response(path, params=params)

    case.erp._get.side_effect = get
    case.erp._get_response.side_effect = response
    return case


def test_existing_pages_preview_and_single_write_share_pdd_write_endpoint(existing):
    c = existing
    assert c.service.run(dry_run=True).ready == 1
    assert c.state['writes'] == 0
    result = c.service.run(dry_run=False, include_details=True)
    assert result.applied == 1, result.details
    assert c.state['writes'] == 1
    assert c.service.run(dry_run=False).scanned == 0
    assert c.state['writes'] == 1
    c.platform.agree_refund.assert_not_called()


def test_live_voyager_checkbox_no_means_only_refund(existing):
    existing.source['isRefundGoods'] = '否'
    existing.admin['是否退货'] = '否'
    assert existing.service.run(dry_run=True).ready == 1


@pytest.mark.parametrize('change', ['detail', 'refundId', 'orderId', 'platform', 'applyPayment',
                                    'applyCarriage', 'isRefundGoods', 'ddnr', 'csname', 'log'])
def test_existing_detail_mismatch_or_missing_field_blocks_money(existing, change):
    c = existing
    if change == 'detail':
        del c.source[change]
    else:
        c.source[change] = 'unexpected'
    result = c.service.run(dry_run=False, include_details=True)
    assert result.blocked == 1 and result.applied == 0, result.details
    assert c.state['writes'] == 0


@pytest.mark.parametrize('corruption', ['duplicate_label', 'truncated', 'login', 'missing_body'])
def test_detail_structure_cannot_be_guessed(existing, corruption):
    c = existing
    original = c.erp._get.side_effect

    def get(path, *, params):
        page = original(path, params=params)
        if not path.endswith('/admin/refunds/77'):
            return page
        if corruption == 'duplicate_label':
            return page.replace('<h3>Detail</h3>', '<h3>平台单号</h3>')
        if corruption == 'truncated':
            return page[:-20]
        if corruption == 'login':
            return '<html><body>权限不足</body></html>'
        return page.replace('class="panel-body"', 'class="missing-body"', 1)

    c.erp._get.side_effect = get
    with pytest.raises(ValueError):
        read_existing_refund(c.erp, OID)
    assert c.state['writes'] == 0


def test_filtered_list_must_be_unique_and_complete(existing):
    c = existing
    c.erp._get = Mock(return_value=table(list(c.admin), [c.admin, c.admin]))
    with pytest.raises(ValueError, match='不唯一'):
        read_existing_refund(c.erp, OID)
    assert c.erp._get.call_count == 1


def test_existing_server_refusal_is_not_success_or_automatically_retried(existing):
    c = existing
    original = c.erp._get_response.side_effect

    def refuse(path, *, params):
        result = original(path, params=params)
        c.state['completed'] = False
        return result

    c.erp._get_response.side_effect = refuse
    first = c.service.run(dry_run=False)
    assert first.blocked == 1 and first.applied == 0
    second = c.service.run(dry_run=False, platform_order_sn=OID)
    assert second.blocked == 1 and c.state['writes'] == 1
