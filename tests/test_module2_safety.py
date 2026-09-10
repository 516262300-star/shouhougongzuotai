from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aftersales_workbench.db.models import ItemStatus, WarehouseReturnItem, WarehouseReturnRecord
from aftersales_workbench.integrations.erp.return_match import ErpReturnMatchStatus
from aftersales_workbench.workflows.module2_safety import require_erp_receipt, require_receipt
from tests import test_uncollected_refund as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def receipt_case(db):
    order, _ = base.sample.__wrapped__(db)
    order.return_tracking_number = "RETURN-EXAMPLE"
    receipt = WarehouseReturnRecord(
        id=1,
        receipt_sn="RECEIPT-EXAMPLE",
        return_tracking_number="RETURN-EXAMPLE",
        after_sales_sn=order.after_sales_sn,
        destination="CUSTOMER_PROFILE",
        inspection_status="PASS",
        inspected_by="warehouse-test",
        inspected_at=base.NOW,
        operator="warehouse-test",
        request_hash="example",
        items=[
            WarehouseReturnItem(
                product_code="test-128", color="silver", quantity=2, item_status=ItemStatus.NORMAL
            )
        ],
    )
    db.add(receipt)
    db.commit()
    return order, receipt, SimpleNamespace(payload={"warehouse_return_id": 1})


@pytest.mark.parametrize(
    "mutation",
    [
        "short",
        "excess",
        "wrong_sku",
        "wrong_color",
        "damaged",
        "system_pass",
        "no_inspector",
        "no_time",
        "unknown_quality",
        "wrong_tracking",
        "occupied",
    ],
)
def test_unsafe_warehouse_receipt_blocks_refund(db, receipt_case, mutation):
    order, receipt, task = receipt_case
    item = receipt.items[0]
    if mutation == "short":
        item.quantity = 1
    elif mutation == "excess":
        item.quantity = 3
    elif mutation == "wrong_sku":
        item.product_code = "test-129"
    elif mutation == "wrong_color":
        item.color = "black"
    elif mutation == "damaged":
        item.item_status = ItemStatus.DEFECTIVE
    elif mutation == "system_pass":
        receipt.inspected_by = "系统ERP核对"
    elif mutation == "no_inspector":
        receipt.inspected_by = None
    elif mutation == "no_time":
        receipt.inspected_at = None
    elif mutation == "unknown_quality":
        receipt.inspection_status = "PENDING"
    elif mutation == "wrong_tracking":
        order.return_tracking_number = "OTHER-TRACKING"
    else:
        receipt.after_sales_sn = None
    db.commit()
    with pytest.raises(ValueError):
        require_receipt(db, order, task)


def test_matching_receipt_is_checked_against_live_erp(db, receipt_case, monkeypatch):
    order, receipt, task = receipt_case
    matcher = Mock()
    matcher.lookup.return_value = SimpleNamespace(
        return_order_sn=receipt.receipt_sn,
        status=ErpReturnMatchStatus.STAGED,
    )
    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_safety.build_erp_return_matcher",
        lambda settings: matcher,
    )
    require_erp_receipt(db, object(), order, task)
    assert matcher.lookup.call_args.kwargs["tracking_number"] == "RETURN-EXAMPLE"
    assert matcher.lookup.call_args.kwargs["expected_items"][0].quantity == 2
    matcher.close.assert_called_once()


@pytest.mark.parametrize("cause", ["timeout", "permission", "wrong_receipt"])
def test_unconfirmed_erp_receipt_blocks_and_closes_client(db, receipt_case, monkeypatch, cause):
    order, receipt, task = receipt_case
    matcher = Mock()
    if cause == "wrong_receipt":
        matcher.lookup.return_value = SimpleNamespace(
            return_order_sn="OTHER-RECEIPT",
            status=ErpReturnMatchStatus.STAGED,
        )
    else:
        matcher.lookup.side_effect = TimeoutError() if cause == "timeout" else PermissionError()
    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_safety.build_erp_return_matcher",
        lambda settings: matcher,
    )
    with pytest.raises((ValueError, TimeoutError, PermissionError)):
        require_erp_receipt(db, object(), order, task)
    matcher.close.assert_called_once()
