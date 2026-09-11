from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from aftersales_workbench.integrations.erp.closure import verify_closure
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpWebUnshippedRefundClient,
    _find_pending_refund,
)
from tests.test_erp_closure_guard import lookup, order
from tests.test_erp_unshipped_refund import AFTER_SALES_SN, ORDER_SN, _pending_page
from tests.test_module1_erp_refund import _FakeService, _FakeSession, _task_order


def table(headers, rows):
    return (
        "<table><tr>"
        + "".join(f"<th>{h}</th>" for h in headers)
        + "</tr>"
        + "".join(
            "<tr>" + "".join(f"<td>{r.get(h, '')}</td>" for h in headers) + "</tr>" for r in rows
        )
        + "</table>"
    )


class ReadOnlyClient:
    _parse_receivable = staticmethod(ErpWebUnshippedRefundClient._parse_receivable)
    _parse_outstanding_items = staticmethod(ErpWebUnshippedRefundClient._parse_outstanding_items)
    _parse_refund_reference = staticmethod(ErpWebUnshippedRefundClient._parse_refund_reference)

    def __init__(self):
        self.records = [
            {
                "平台单号": "ORDER-1",
                "退款单号": "AF-1",
                "平台": "天猫",
                "状态": "退款成功",
                "退款金额": "3.29",
                "系统订单号": "DD-1",
                "系统客户名称": "CUSTOMER-1",
            }
        ]
        self.receipts = [
            {
                "单据编号": "SK-SALE",
                "收款金额": "3.29",
                "制单人": "DD-1",
                "备注": "原收款",
                "订单编号": "1",
            },
            {
                "单据编号": "SK-REFUND",
                "收款金额": "-3.29",
                "制单人": "AF-1",
                "备注": "自动开退款单DD-1",
                "订单编号": "1",
            },
        ]
        self.balance = "0"
        self.calls = []

    def _get(self, path, *, params):
        self.calls.append((path, params))
        assert path == "/leedis2/public/admin/refunds"  # 禁止待补单接口、平台退款或 ERP 写操作
        assert params["s"] == "ORDER-1" and params["filter"] == "equals"
        return table(list(self.records[0]), self.records)

    def _load_customer_profile(self, platform_order_sn, expected_customer):
        assert (platform_order_sn, expected_customer) == ("ORDER-1", "CUSTOMER-1")
        profile = table(
            ["客户名字", "累计应收"], [{"客户名字": "CUSTOMER-1", "累计应收": self.balance}]
        )
        profile += table(["订单编号", "型号", "完整颜色", "欠货量"], [])
        profile += table(["单据编号", "收款金额", "制单人", "备注", "订单编号"], self.receipts)
        return profile, "1"


def tmall_order():
    o = order()
    o.shop.platform = "TMALL"
    o.refund_amount = o.platform_order_amount = o.platform_goods_amount = Decimal("3.29")
    o.merchant_receivable_amount = None
    return o


def test_tmall_full_return_with_exact_original_and_refund_receipts_closes_readonly():
    c = ReadOnlyClient()
    result = verify_closure(tmall_order(), lookup(), c)
    assert result.status == "closed_loop" and result.closure_evidence.platform == "TMALL"
    assert result.closure_evidence.reference_sn == "SK-REFUND"
    assert len(c.calls) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "wrong_platform",
        "wrong_order",
        "wrong_after",
        "wrong_amount",
        "duplicate_order",
        "missing_sale",
        "duplicate_refund",
        "balance",
    ],
)
def test_tmall_missing_or_ambiguous_accounting_cannot_close(kind):
    c = ReadOnlyClient()
    changes = {
        "wrong_platform": ("平台", "拼多多"),
        "wrong_order": ("平台单号", "OTHER"),
        "wrong_after": ("退款单号", "OTHER"),
        "wrong_amount": ("退款金额", "9.99"),
    }
    if kind in changes:
        k, v = changes[kind]
        c.records[0][k] = v
    elif kind == "duplicate_order":
        c.records.append(deepcopy(c.records[0]))
    elif kind == "missing_sale":
        c.receipts.pop(0)
    elif kind == "duplicate_refund":
        c.receipts.append({**c.receipts[1], "收款金额": "-1.00", "单据编号": "SK-EXTRA"})
    else:
        c.balance = "-3.29"
    assert verify_closure(tmall_order(), lookup(), c).status == "refund_unverified"


def test_unknown_tmall_coupon_amount_is_not_guessed():
    o = tmall_order()
    o.platform_goods_amount = Decimal("4.29")
    c = ReadOnlyClient()
    assert verify_closure(o, lookup(), c).status == "refund_unverified" and not c.calls


def test_non_pdd_never_enters_money_supplement_workflow():
    task, o = _task_order()
    o.shop.platform = "TMALL"
    result = _FakeService(_FakeSession(), object(), object(), [(task, o)]).run(dry_run=False)
    assert result.refund_unverified == 1 and result.applied == 0


def test_return_tracking_is_allowed_only_for_shipped_preflight():
    pending = _find_pending_refund(_pending_page(), platform_order_sn=ORDER_SN)
    pending = replace(pending, return_tracking_number="RETURN-TRACK")
    c = ErpWebUnshippedRefundClient(
        base_url="https://example.invalid", username="test", password="test"
    )
    try:
        kwargs = {"after_sales_sn": AFTER_SALES_SN, "expected_amount": Decimal("74.51")}
        assert (
            c._validate_pending(pending, **kwargs) == "ERP 待处理记录存在退货运单，不属于未发货退款"
        )
        assert c._validate_pending(pending, **kwargs, shipped_return=True) is None
        assert "金额" in c._validate_pending(
            pending, **{**kwargs, "expected_amount": Decimal("1")}, shipped_return=True
        )
    finally:
        c.close()
