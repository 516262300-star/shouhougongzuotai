import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesItem, Platform
from aftersales_workbench.integrations.pdd.mapper import (
    PddDataMappingError,
    normalize_refund,
    normalize_refund_item,
)
from aftersales_workbench.integrations.pdd.repository import SqlAlchemyPddSyncRepository
from aftersales_workbench.services.return_quantity import (
    correct_legacy_quantity_failure,
    quantity_review_note,
    queued_quantity_review,
)
from aftersales_workbench.workflows.module2_erp_intake import Module2ExceptionTodoService
from tests import test_module2_todo_queue as fixtures


@pytest.fixture
def db():
    yield from fixtures.db.__wrapped__()


def test_count_comes_from_partial_fields_not_purchase_or_money():
    item = normalize_refund_item("sample", {
        "goods_number": 34, "part_after_sales_type": 1, "part_after_sales_value": 10,
        "refund_amount": 5420, "goods_price": 549,
    }, {})
    assert (item.applied_quantity, item.purchased_quantity) == (10, 34)
    assert item.quantity_source == "PDD_PART_AFTER_SALES"


@pytest.mark.parametrize("value", [None, 0, -1, 35, "10.1", "NaN", "Infinity", True, "bad"])
def test_invalid_partial_count_is_quarantined(value):
    with pytest.raises(PddDataMappingError):
        normalize_refund_item("sample", {
            "goods_number": 34, "part_after_sales_type": 1, "part_after_sales_value": value,
        }, {})


def test_unknown_type_does_not_guess_and_missing_type_keeps_unknown_purchase():
    with pytest.raises(PddDataMappingError):
        normalize_refund_item("sample", {"goods_number": 34, "part_after_sales_type": 9}, {})
    item = normalize_refund_item("sample", {"goods_number": 34, "part_after_sales_value": 10}, {})
    assert item.applied_quantity == 34 and item.quantity_source == "PDD_GOODS_NUMBER"


def known_partial(db, status="SUCCEEDED"):
    order, receipt = fixtures.add_partial_return(db, task_status=status)
    item = order.items[0]
    item.purchased_quantity = 51
    item.applied_quantity = 27
    item.quantity_source = "PDD_PART_AFTER_SALES"
    item.item_status = "DEFECTIVE"
    db.commit()
    return order, receipt


def test_existing_success_todo_no_longer_excludes_legacy_correction(db):
    order, receipt = known_partial(db)
    before = list(db.execute(select(AftersalesActionTask.__table__)).mappings())
    service = Module2ExceptionTodoService(db)
    preview = service.run(dry_run=True)
    assert preview.quantity_reviews == 1 and receipt.inspection_status == "FAIL"
    result = service.run(dry_run=False)
    assert result.quantity_reviews == 1 and result.tasks_created == 0
    assert receipt.inspection_status == "PENDING" and receipt.inspected_by is None
    assert "实收一致" in receipt.inspection_note and "少退或未收到" not in receipt.inspection_note
    assert "少退或未收到" in receipt.note and "DEFECTIVE" in receipt.note
    assert order.items[0].item_status is None
    assert order.workflow_status == "MANUAL_PROCESSING"
    assert list(db.execute(select(AftersalesActionTask.__table__)).mappings()) == before
    audit = receipt.note
    assert service.run(dry_run=False).quantity_reviews == 0
    assert receipt.note == audit


def test_corrected_receipt_cannot_release_old_pending_false_todo(db):
    order, receipt = known_partial(db, status="PENDING")
    Module2ExceptionTodoService(db).run(dry_run=False)
    note = queued_quantity_review(
        db, {"origin": "module2", "reason_code": "RETURN_ITEM_MISMATCH"}, order.after_sales_sn,
    )
    assert note == receipt.inspection_note


@pytest.mark.parametrize("change", [
    "real_shortage", "manual_quality", "additional_quality", "wrong_color",
])
def test_never_reclassify_real_or_independent_failure(db, change):
    order, receipt = known_partial(db)
    if change == "real_shortage":
        order.items[0].applied_quantity = 28
    elif change == "manual_quality":
        receipt.inspected_by = "仓库质检"
    elif change == "additional_quality":
        receipt.inspection_note += "表面划伤"
    else:
        receipt.items[0].color = "other"
    assert correct_legacy_quantity_failure(db, order, Platform.PDD) is None
    assert receipt.inspection_status == "FAIL"


def test_proven_partial_quantity_does_not_suppress_real_shortage(db):
    order, receipt = known_partial(db)
    receipt.items[0].quantity = 26
    assert quantity_review_note(order, Platform.PDD, receipt.items) is None


def test_repository_persists_quantity_and_source_on_resync(db):
    raw = {"id": 999, "order_sn": "sample-order", "after_sales_type": 2,
           "goods_number": 34, "out_sku_sn": "sample-sku", "refund_amount": 5420,
           "part_after_sales_type": 1, "part_after_sales_value": 10}
    refund = normalize_refund({}, raw, {})
    repository = SqlAlchemyPddSyncRepository(db)
    repository.upsert_refund(1, refund)
    db.commit()
    repository.upsert_refund(1, refund)
    db.commit()
    item = db.scalar(select(AfterSalesItem).where(AfterSalesItem.after_sales_sn == "999"))
    assert (item.applied_quantity, item.purchased_quantity, item.quantity_source) == (
        10, 34, "PDD_PART_AFTER_SALES",
    )
