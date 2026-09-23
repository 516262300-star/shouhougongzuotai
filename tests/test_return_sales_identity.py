from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aftersales_workbench.db.models import Platform, WorkflowStatus
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnRow,
)
from aftersales_workbench.integrations.erp.shared_returns import ShipmentRow
from aftersales_workbench.services.return_sales_identity import (
    SALES_IDENTITY_REVIEW,
    matches_original_sale,
    original_sale_review,
)
from aftersales_workbench.workflows.module2_erp_intake import (
    Module2ErpIntakeRunResult,
    Module2ErpIntakeService,
)


def evidence():
    rows = (
        ShipmentRow("1", "RC-1", "sale-1", "order-1", "把手", "铬", Decimal(6)),
        ShipmentRow("2", "RC-1", "sale-1", "order-1", "底座", "铬", Decimal(6)),
        ShipmentRow("3", "TH-1", "track-1", "sale-1", "把手", "铬", Decimal(6)),
        ShipmentRow("4", "TH-1", "track-1", "sale-1", "底座", "铬", Decimal(6)),
    )
    lookup = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.ITEM_MISMATCH, message="平台为组合商品",
        customer_name="测试客户", source_location="customer_profile", return_order_sn="TH-1",
        rows=tuple(ErpReturnRow(
            return_order_sn=r.document, completed_at="2026-09-23", product=r.product,
            color=r.color, tracking_number=r.order_ref, quantity=r.quantity,
            unit_price=None, amount=None,
        ) for r in rows if r.returned),
    )
    order = SimpleNamespace(
        platform_order_sn="order-1", return_tracking_number="track-1",
        items=[], workflow_status=WorkflowStatus.PENDING_CHECK,
        exception_type=None, refund_financial_status="SUCCESS",
    )
    return rows, lookup, order


def test_original_sale_components_match_without_global_sku_alias():
    rows, lookup, _ = evidence()
    assert matches_original_sale(rows, lookup, "order-1", "track-1")
    assert not matches_original_sale(rows, lookup, "other-order", "track-1")


@pytest.mark.parametrize("change", [
    {"customer_ref": "other-sale"}, {"color": "黑"}, {"quantity": Decimal(5)},
    {"quantity": Decimal(7)}, {"product": "其他型号"}, {"row_id": "3"},
    {"document": "TH-other"}, {"order_ref": "other-track"},
])
def test_rejects_mismatch_or_ambiguous_lineage(change):
    rows, lookup, _ = evidence()
    rows = (*rows[:-1], replace(rows[-1], **change))
    assert not matches_original_sale(rows, lookup, "order-1", "track-1")


def test_rejects_return_already_present_in_another_parcel():
    rows, lookup, _ = evidence()
    rows += (replace(rows[-1], row_id="5", document="TH-2", order_ref="track-2"),)
    assert not matches_original_sale(rows, lookup, "order-1", "track-1")


def test_rejects_sales_id_shared_with_other_order():
    rows, lookup, _ = evidence()
    rows += (replace(rows[0], row_id="5", customer_ref="other-order"),)
    assert not matches_original_sale(rows, lookup, "order-1", "track-1")


def test_rejects_changed_receipt_between_reads():
    rows, lookup, _ = evidence()
    lookup = replace(lookup, rows=(replace(lookup.rows[0], quantity=Decimal(5)), *lookup.rows[1:]))
    assert not matches_original_sale(rows, lookup, "order-1", "track-1")


@pytest.mark.parametrize("dry_run", [True, False])
def test_intake_does_not_create_failure_or_quality_pass(dry_run):
    rows, lookup, order = evidence()
    matcher = SimpleNamespace(lookup=lambda **kwargs: lookup, _get=lambda *args, **kwargs: None)
    result = Module2ErpIntakeRunResult(dry_run=dry_run)
    with patch("aftersales_workbench.services.return_sales_identity.read_customer_rows",
               return_value=(rows, 1)), patch(
                   "aftersales_workbench.services.return_sales_identity.save_sales_evidence"
               ) as save:
        note = Module2ErpIntakeService(None, matcher)._inspect_candidate(
            order, Platform.TMALL, set(), result, dry_run)
    assert save.call_count == int(not dry_run)
    assert note == SALES_IDENTITY_REVIEW
    assert result.original_sales_reviews == 1
    assert result.inspections_failed == result.inspections_passed == result.receipts_created == 0
    assert order.workflow_status == (WorkflowStatus.PENDING_CHECK if dry_run
                                     else WorkflowStatus.MANUAL_PROCESSING)


def test_incomplete_customer_pages_propagates_without_creating_failure():
    _, lookup, order = evidence()
    matcher = SimpleNamespace(_get=lambda *args, **kwargs: None)
    with patch("aftersales_workbench.services.return_sales_identity.read_customer_rows",
               side_effect=ValueError("分页不完整")), pytest.raises(ValueError):
        original_sale_review(matcher, order, lookup)
