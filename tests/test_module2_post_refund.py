from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from aftersales_workbench.db.models import Platform, WorkflowStatus
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnRow,
)
from aftersales_workbench.workflows.module2_erp_intake import (
    Module2ErpIntakeRunResult,
    Module2ErpIntakeService,
)


def sample():
    order = NS(
        after_sales_sn="refund",
        platform_order_sn="order",
        shop_id=1,
        return_tracking_number="tracking",
        merchant_receivable_amount=Decimal("10"),
        erp_customer_name="customer",
        refund_financial_status="SUCCESS",
        platform_after_sales_status=10,
        platform_order_refund_status=4,
        workflow_status=WorkflowStatus.MANUAL_PROCESSING,
        exception_type="旧质检提示",
        items=[NS(sku_code="sku", color="red", applied_quantity=1)],
    )
    lookup = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.REFUND_UNVERIFIED,
        message="matched",
        customer_name="customer",
        receivable_amount=Decimal("0"),
        return_order_sn="TH-1",
        source_location="customer_profile",
        rows=(
            ErpReturnRow(
                "TH-1",
                "today",
                "sku",
                "red",
                "tracking",
                Decimal("1"),
                Decimal("10"),
                Decimal("-10"),
            ),
        ),
    )
    bill = NS(
        status="completed",
        platform_order_sn="order",
        customer_name="customer",
        refund_amount=Decimal("10"),
        receivable_amount=Decimal("0"),
        outstanding_items=(),
        erp_order_sn="DD-1",
        reference_sn="SK-1",
    )
    return order, lookup, bill


def run(order, lookup, bill, dry_run=True, shared=None):
    matcher = NS(lookup=lambda **kw: lookup, inspect_post_refund_bill=lambda *args: bill)
    service = Module2ErpIntakeService(NS(), matcher)
    service._record = lambda *args: pytest.fail("不得生成验货通过或资金任务")
    result = Module2ErpIntakeRunResult(dry_run=dry_run)
    error = service._inspect_candidate(order, Platform.PDD, shared or set(), result, dry_run)
    return error, result


def test_settled_refund_records_accounting_without_quality_or_money(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_post_refund.save_evidence", saved.append
    )
    order, lookup, bill = sample()
    error, result = run(order, lookup, bill, dry_run=False)
    assert error is None and result.post_refund_verified == 1 and result.unavailable == 0
    assert order.workflow_status == WorkflowStatus.RETURN_RECEIVED_ASSIGNED
    assert "非质检结论" in order.exception_type
    assert saved[0]["quality_verified"] is False
    assert result.inspections_passed == 0


def test_preview_does_not_change_order():
    order, lookup, bill = sample()
    assert run(order, lookup, bill)[0] is None
    assert order.exception_type == "旧质检提示"


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference_sn", None),
        ("refund_amount", Decimal("9")),
        ("receivable_amount", Decimal("1")),
        ("customer_name", "other"),
        ("status", "unavailable"),
    ],
)
def test_zero_customer_balance_alone_is_not_verification(field, value):
    order, lookup, bill = sample()
    setattr(bill, field, value)
    error, result = run(order, lookup, bill)
    assert error and result.post_refund_verified == 0


def test_unpaid_refund_keeps_quality_gate_and_shared_tracking_protection():
    order, lookup, bill = sample()
    order.refund_financial_status = "PENDING"
    order.platform_after_sales_status = 3
    order.platform_order_refund_status = 2
    assert "质检" in run(order, lookup, bill)[0]
    assert "同退货运单" in run(order, lookup, bill, shared={"tracking"})[0]


def test_staged_return_is_not_settled():
    order, lookup, bill = sample()
    error, result = run(
        order, replace(lookup, source_location="staging", status=ErpReturnMatchStatus.STAGED), bill
    )
    assert "待认领" in error and result.post_refund_verified == 0
