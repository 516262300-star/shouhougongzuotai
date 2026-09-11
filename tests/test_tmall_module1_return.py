"""全部为合成订单和模拟ERP响应；不会执行生产资金请求。"""

from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import (
    AutomationActionType as Action,
)
from aftersales_workbench.db.models import (
    MoneyOperation,
    ShippingStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.tmall_returned import inspect_return_account
from aftersales_workbench.workflows.tmall_module1_return import TmallModule1ReturnService
from tests import test_tmall_module3 as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def case(db, monkeypatch):
    c = base.case.__wrapped__(db)
    c.order.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
    c.order.order_shipping_status = ShippingStatus.IN_TRANSIT
    c.order.platform_order_amount = Decimal("20")
    c.order.platform_goods_amount = Decimal("20")
    c.order.forward_tracking_number = "TRACK"
    c.task.action_type = Action.ERP_MATCH_RETURN_ORDER
    c.cfg.tmall_module1_return_claim_enabled = True
    c.cfg.erp_automation_account_dedicated = True
    c.cfg.erp_web_lookup_enabled = True
    c.cfg.erp_return_match_sync_enabled = True
    c.cfg.module1_erp_refund_execution_enabled = True
    c.db.commit()
    monkeypatch.setattr(
        "aftersales_workbench.workflows.tmall_module1_return.verify_tmall_refund",
        lambda *a, **k: c.refund,
    )
    c.account = dict(
        state="ready",
        record_id="77",
        erp_order="DD-11",
        customer="测试客户",
        price="10",
        receipt="TH-123-2026-09-11",
        reference=None,
        source={},
        sale={},
        return_row={"编号": "TH-123-2026-09-11"},
    )

    def account(*args, **kwargs):
        return {
            **deepcopy(c.account),
            **({"state": "completed", "reference": "SK-REFUND"} if c.state["completed"] else {}),
        }

    monkeypatch.setattr(
        "aftersales_workbench.workflows.tmall_module1_return.inspect_return_account", account
    )
    verifier = Mock()
    verifier.inspect.return_value = {"customer_id": "501"}
    c.service = TmallModule1ReturnService(
        db, c.erp, Mock(), c.cfg, platform_client_factory=lambda _: c.platform, verifier=verifier
    )
    c.service._close = Mock()

    def write(path, **kwargs):
        assert path.endswith("/deleteprodlist/77") and kwargs["follow_redirects"] is False
        assert c.db.scalar(select(MoneyOperation)).state == "REQUEST_STARTED"
        c.state["writes"] += 1
        c.state["completed"] = True
        if c.state["timeout"]:
            raise TimeoutError("synthetic unknown")
        return Mock()

    c.erp._client.get.side_effect = write
    return c


def test_preview_does_not_claim_or_write_money(case):
    result = case.service.run()
    assert result["ready"] == 1 and result["applied"] == 0, result
    assert case.state["writes"] == 0
    assert case.db.scalar(select(MoneyOperation)) is None
    case.platform.agree_refund.assert_not_called()


def test_module1_money_ledger_committed_before_single_request(case):
    result = case.service.run(dry_run=False)
    assert result["applied"] == 1 and result["blocked"] == 0, result
    assert case.state["writes"] == 1
    assert case.db.scalar(select(MoneyOperation)).state == "CONFIRMED"
    case.platform.agree_refund.assert_not_called()


def test_unknown_financial_request_never_retried(case):
    case.state["timeout"] = True
    result = case.service.run(dry_run=False)
    assert result["blocked"] == 1 and case.state["writes"] == 1
    assert case.db.scalar(select(MoneyOperation)).state == "UNKNOWN"
    case.state["completed"] = False
    result = case.service.run(dry_run=False, platform_order_sn=base.OID)
    assert result["blocked"] == 1 and case.state["writes"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "sixth_shop",
        "not_success",
        "partial_amount",
        "multi_parcel",
        "dedicated_off",
        "missing_actual",
        "unshipped",
    ],
)
def test_gates_cannot_be_bypassed(case, change):
    if change == "sixth_shop":
        case.shop.shop_code = "tmall-shop-06"
    elif change == "not_success":
        case.refund["status"] = "WAIT_SELLER_AGREE"
    elif change == "partial_amount":
        case.trade["payment"] = "25"
    elif change == "multi_parcel":
        case.service.verifier.inspect.side_effect = ValueError("多包裹须人工核验")
    elif change == "dedicated_off":
        case.cfg.erp_automation_account_dedicated = False
    elif change == "missing_actual":
        case.order.actual_refund_amount = None
    elif change == "unshipped":
        case.order.order_shipping_status = ShippingStatus.UNSHIPPED
    case.db.commit()
    if change == "dedicated_off":
        with pytest.raises(ValueError):
            case.service.run(dry_run=False)
    else:
        result = case.service.run(dry_run=False)
        assert result["applied"] == 0
    assert case.state["writes"] == 0


@pytest.fixture
def account_case(db, monkeypatch):
    c = base.case.__wrapped__(db)
    c.outstanding.clear()
    c.balance["累计应收"] = "-20"
    c.rows = [
        dict(
            编号="RC-1",
            型号="MODEL-96",
            颜色="银",
            订单编号="11",
            客户编号=base.OID,
            入库化只="2",
            单价="10",
        ),
        dict(
            编号="TH-123-2026-09-11",
            型号="MODEL-96",
            颜色="银",
            订单编号="TRACK",
            客户编号="11",
            入库化只="-2",
            单价="10",
        ),
    ]
    monkeypatch.setattr(
        "aftersales_workbench.integrations.erp.tmall_returned.read_existing_refund",
        lambda *_: c.source,
    )
    monkeypatch.setattr(
        "aftersales_workbench.integrations.erp.tmall_returned.customer_rows", lambda *_: c.rows
    )

    def profile(*args):
        return (
            base.table(["客户名字", "累计应收"], [c.balance])
            + base.table(["订单编号", "完整颜色", "型号", "欠货量"], [])
            + base.table(list(c.original), c.receipts),
            "501",
        )

    c.erp._load_customer_profile.side_effect = profile
    c.inspect = lambda: inspect_return_account(
        c.erp,
        order_sn=base.OID,
        refund_sn="9001",
        child_id="2001",
        expected=Decimal("20"),
        product="MODEL-96",
        color="银",
        quantity=Decimal("2"),
        customer_id="501",
        tracking="TRACK",
    )
    return c


def test_original_sales_and_actual_return_required(account_case):
    assert account_case.inspect()["state"] == "ready"
    account_case.receipts.append(account_case.receipt)
    account_case.balance["累计应收"] = "0"
    assert account_case.inspect()["state"] == "completed"


@pytest.mark.parametrize(
    "change",
    [
        "wrong_sale",
        "wrong_color",
        "wrong_quantity",
        "zero_price",
        "positive_return",
        "other_return",
        "other_receipt",
        "duplicate_original",
        "wrong_child",
        "wrong_erp_platform",
        "refund_pending",
        "bad_balance",
        "old_write",
    ],
)
def test_unsafe_account_never_ready(account_case, change):
    c = account_case
    if change == "wrong_sale":
        c.rows[1]["客户编号"] = "12"
    elif change == "wrong_color":
        c.rows[1]["颜色"] = "黑"
    elif change == "wrong_quantity":
        c.rows[1]["入库化只"] = "-1"
    elif change == "zero_price":
        c.rows[0]["单价"] = "0"
    elif change == "positive_return":
        c.rows[1]["入库化只"] = "2"
    elif change == "other_return":
        c.rows.append(deepcopy(c.rows[1]))
    elif change == "other_receipt":
        c.receipts.append({**c.original, "制单人": "DD-OTHER"})
    elif change == "duplicate_original":
        c.receipts.append(deepcopy(c.original))
    elif change == "wrong_child":
        c.source["detail"] = '{"other":2}'
    elif change == "wrong_erp_platform":
        c.source["platform"] = "拼多多"
    elif change == "refund_pending":
        c.source["overall_status"] = "处理中"
    elif change == "bad_balance":
        c.balance["累计应收"] = "0"
    elif change == "old_write":
        c.source["log"] = "自动补开中"
    with pytest.raises(ValueError):
        c.inspect()
