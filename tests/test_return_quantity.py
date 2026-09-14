from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from aftersales_workbench.db.models import ItemStatus, Platform, WorkflowStatus
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnRow,
)
from aftersales_workbench.services.return_quantity import (
    legacy_quantity_review_note,
    quantity_review_note,
)
from aftersales_workbench.workflows.module2_erp_intake import (
    Module2ErpIntakeRunResult,
    Module2ErpIntakeService,
)


def sample():
    order = NS(
        after_sales_sn="test-after", platform_order_sn="test-order",
        return_tracking_number="test-tracking", platform_after_sales_status=3,
        refund_financial_status="PENDING", workflow_status=WorkflowStatus.PENDING_CHECK,
        exception_type=None,
        items=[NS(sku_code="sample-sku#铜本色", color=None, applied_quantity=51)],
    )
    actual = [NS(product_code="sample-sku", color="铜本色", quantity=27,
                 item_status=ItemStatus.NORMAL)]
    return order, actual


@pytest.mark.parametrize("refunded", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_partial_return_never_creates_fail_pass_or_money(refunded, dry_run):
    order, actual = sample()
    if refunded:
        order.platform_after_sales_status = 10
        order.refund_financial_status = "SUCCESS"
    lookup = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.ITEM_MISMATCH, message="old quantity mismatch",
        return_order_sn="test-receipt", source_location="customer_profile",
        rows=(ErpReturnRow("test-receipt", "today", "sample-sku", "铜本色",
                           "test-tracking", Decimal(27), Decimal(8), Decimal(-216)),),
    )
    service = Module2ErpIntakeService(NS(), NS(lookup=lambda **kw: lookup))
    service._record = lambda *args: pytest.fail("未知数量不得记录验货通过或失败")
    result = Module2ErpIntakeRunResult(dry_run=dry_run)
    message = service._inspect_candidate(order, Platform.PDD, set(), result, dry_run)
    assert "本次退货数量待核实" in message
    assert "×51" in message and "×27" in message and "×24" not in message
    assert result.quantity_reviews == 1 and result.ambiguous == 1
    assert result.inspections_failed == result.inspections_passed == result.receipts_created == 0
    assert result.post_refund_verified == 0
    assert order.workflow_status == (
        WorkflowStatus.PENDING_CHECK if dry_run else WorkflowStatus.MANUAL_PROCESSING)
    assert order.items[0].applied_quantity == 51


@pytest.mark.parametrize("field,value", [
    ("product_code", "different-sku"), ("color", "铬"), ("quantity", 52),
    ("quantity", 0), ("quantity", Decimal("1.5")),
    ("item_status", ItemStatus.DEFECTIVE),
])
def test_real_item_or_quality_discrepancy_is_not_suppressed(field, value):
    order, actual = sample()
    setattr(actual[0], field, value)
    assert quantity_review_note(order, Platform.PDD, actual) is None


def test_full_return_and_other_platform_keep_existing_flow():
    order, actual = sample()
    assert quantity_review_note(order, Platform.TMALL, actual) is None
    actual[0].quantity = 51
    assert quantity_review_note(order, Platform.PDD, actual) is None
    assert quantity_review_note(order, Platform.PDD, []) is None


@pytest.mark.parametrize("amount", [Decimal("100"), Decimal("200"), None])
def test_no_quantity_inference_from_money(amount):
    order, actual = sample()
    order.refund_amount = amount
    order.platform_order_amount = Decimal("200")
    assert quantity_review_note(order, Platform.PDD, actual)


def test_aggregate_split_erp_rows_before_decision():
    order, actual = sample()
    actual.append(NS(**{**vars(actual[0]), "quantity": 24}))
    assert quantity_review_note(order, Platform.PDD, actual) is None
    actual[1].quantity = 1
    assert "×28" in quantity_review_note(order, Platform.PDD, actual)


def test_never_override_independent_quality_failure():
    order, actual = sample()
    receipt = NS(items=actual, inspection_status="FAIL", inspected_by="仓库质检员")
    assert legacy_quantity_review_note(order, Platform.PDD, receipt) is None
    receipt.inspected_by = "系统ERP核对"
    assert legacy_quantity_review_note(order, Platform.PDD, receipt)
