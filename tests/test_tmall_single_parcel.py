"""恢复核验只用合成数据；不连接生产数据库或任何真实资金接口。"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.erp.package_orders import CustomerSales
from aftersales_workbench.workflows.tmall_single_parcel import TmallSingleParcelVerifier


@pytest.fixture
def case():
    order = SimpleNamespace(
        id=1, shop_id=1, after_sales_sn="9001", platform_order_sn="1234567890123456789",
        after_sales_type="ONLY_REFUND", refund_amount=Decimal("20"),
        forward_tracking_number="PACKAGE-1", carrier_code="测试快递",
        return_tracking_number=None, erp_customer_name="测试客户",
        items=[SimpleNamespace(sku_code="MODEL-96#银", color=None, applied_quantity=2)],
    )
    sales = CustomerSales("1", "测试客户", "测试业务员", (
        dict(order_sn=order.platform_order_sn, sale_sn="RC-1", sale_id="1",
             product="MODEL-96", color="银", quantity="2"),
    ), 1)
    source = Mock(read=Mock(return_value=sales))
    detail = dict(refund_id="9001", tid=order.platform_order_sn, oid="1001", num=2)
    trade = dict(tid=order.platform_order_sn, orders={"order": [
        dict(oid="1001", outer_sku_id="MODEL-96#银", num=2),
    ]})
    logistics = {"logistics_orders_get_response": {"shippings": {"shipping": [
        dict(out_sid="PACKAGE-1", company_name="测试快递", status="SENT"),
    ]}}}
    client = Mock()
    client.get_refund.side_effect = lambda **kw: {"refund_get_response": {"refund": detail}}
    client.get_trade_fullinfo.side_effect = lambda **kw: {
        "trade_fullinfo_get_response": {"trade": trade},
    }
    client.get_logistics_orders.side_effect = lambda **kw: logistics
    session = Mock(scalar=Mock(return_value=None))
    verifier = TmallSingleParcelVerifier(session, Settings(_env_file=None),
                                         source_factory=lambda: source)
    return SimpleNamespace(**locals())


def test_complete_independent_parcel_passes_without_writes(case):
    proof = case.verifier.inspect(case.order, case.client)
    assert proof["result"] == "PASS" and proof["pages"] == 1
    assert proof["sales_rows"] == list(case.sales.rows)
    case.client.agree_refund.assert_not_called()
    case.session.commit.assert_not_called()
    case.source.close.assert_called_once()


@pytest.mark.parametrize("change", [
    "other_customer", "other_order", "no_sales", "duplicate_sale", "wrong_color",
    "wrong_quantity", "nan_quantity", "multiple_children", "no_children", "wrong_tid",
    "wrong_refund", "wrong_oid", "no_sku", "partial_quantity", "multiple_packages",
    "changed_tracking", "missing_carrier", "conflicting_aftersale", "timeout",
])
def test_uncertain_or_shared_parcel_blocks(case, change):
    c = case
    if change == "other_customer":
        c.order.erp_customer_name = "另一客户"
    elif change == "other_order":
        c.source.read.return_value = replace(c.sales, rows=(
            *c.sales.rows, {**c.sales.rows[0], "order_sn": "9999999999999999999"},
        ))
    elif change == "no_sales":
        c.source.read.return_value = replace(c.sales, rows=())
    elif change == "duplicate_sale":
        c.source.read.return_value = replace(c.sales, rows=c.sales.rows * 2)
    elif change in {"wrong_color", "wrong_quantity", "nan_quantity"}:
        row = dict(c.sales.rows[0])
        row["color" if change == "wrong_color" else "quantity"] = {
            "wrong_color": "金", "wrong_quantity": "1", "nan_quantity": "NaN",
        }[change]
        c.source.read.return_value = replace(c.sales, rows=(row,))
    elif change == "multiple_children":
        c.trade["orders"]["order"] *= 2
    elif change == "no_children":
        c.trade["orders"]["order"] = []
    elif change == "wrong_tid":
        c.trade["tid"] = "999"
    elif change == "wrong_refund":
        c.detail["refund_id"] = "999"
    elif change == "wrong_oid":
        c.detail["oid"] = "999"
    elif change == "no_sku":
        c.trade["orders"]["order"][0].pop("outer_sku_id")
    elif change == "partial_quantity":
        c.detail["num"] = 1
    elif change == "multiple_packages":
        shipments = c.logistics["logistics_orders_get_response"]["shippings"]["shipping"]
        shipments.append(deepcopy(shipments[0]))
    elif change == "changed_tracking":
        c.order.forward_tracking_number = "OTHER"
    elif change == "missing_carrier":
        c.logistics["logistics_orders_get_response"]["shippings"]["shipping"][0].pop(
            "company_name"
        )
    elif change == "conflicting_aftersale":
        c.session.scalar.side_effect = [None, 2]
    elif change == "timeout":
        c.source.read.side_effect = TimeoutError("offline timeout")
    with pytest.raises((ValueError, TimeoutError)):
        c.verifier.inspect(c.order, c.client)
    c.client.agree_refund.assert_not_called()
    c.session.commit.assert_not_called()
    c.source.close.assert_called_once()


def test_persistent_manual_hold_is_not_released_by_restoration(case):
    case.session.scalar.return_value = 1
    with pytest.raises(ValueError, match="人工处理锁"):
        case.verifier.inspect(case.order, case.client)
    case.source.read.assert_not_called()
    case.client.agree_refund.assert_not_called()


def test_failed_receipt_or_package_never_reaches_money(case, monkeypatch):
    from aftersales_workbench.db.models import AutomationActionType, AutomationTaskStatus
    from aftersales_workbench.workflows import actions
    from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot

    c = case
    c.order.after_sales_type = "RETURN_AND_REFUND"
    c.order.return_tracking_number = "RETURN-1"
    task = ExternalTaskSnapshot(1, c.order.after_sales_sn,
                               AutomationActionType.TMALL_AGREE_RETURN_REFUND,
                               {"origin": "module2"}, c.order.platform_order_sn,
                               "tmall-shop-01")
    c.session.scalar.return_value = c.order
    c.session.get.return_value = SimpleNamespace(
        action_status=AutomationTaskStatus.RUNNING, payload=task.payload,
    )
    executor = ExternalActionExecutor(c.session, Settings(
        _env_file=None, tmall_single_parcel_refund_enabled=True,
    ))
    monkeypatch.setattr(executor, "_require_final_refund_gate", lambda *a: None)
    monkeypatch.setattr(actions, "require_sync_safe_order", lambda *a: None)
    monkeypatch.setattr(actions, "verify_tmall_refund", lambda *a, **kw: {
        "status": "WAIT_SELLER_CONFIRM_GOODS",
    })
    monkeypatch.setattr(TmallSingleParcelVerifier, "inspect",
                        Mock(side_effect=ValueError("多包裹")))
    funds = Mock()
    monkeypatch.setattr(actions, "run_money_write", funds)
    with pytest.raises(ValueError, match="多包裹"):
        executor._execute_tmall_refund(c.client, object(), task)
    funds.assert_not_called()
    c.client.agree_refund.assert_not_called()


def test_restoration_requires_separate_explicit_rollout_switch(case):
    from aftersales_workbench.db.models import AutomationActionType
    from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot

    c = case
    c.session.scalar.return_value = c.order
    executor = ExternalActionExecutor(c.session, Settings(_env_file=None))
    task = ExternalTaskSnapshot(1, c.order.after_sales_sn, AutomationActionType.TMALL_AGREE_REFUND,
                               {"origin": "module1"}, c.order.platform_order_sn,
                               "tmall-shop-01")
    with pytest.raises(ValueError, match="尚未启用"):
        executor._execute_tmall_refund(c.client, object(), task)
    c.client.get_refund.assert_not_called()
    c.client.agree_refund.assert_not_called()
