from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder, Platform, Shop
from aftersales_workbench.integrations.tmall.mapper import normalize_refund
from aftersales_workbench.integrations.tmall.repository import SqlAlchemyTmallSyncRepository
from aftersales_workbench.services.return_quantity import correct_legacy_quantity_failure
from aftersales_workbench.workflows.module2_erp_intake import Module2ExceptionTodoService
from tests import test_module2_todo_queue as fixtures


@pytest.fixture
def db():
    yield from fixtures.db.__wrapped__()


def refund():
    # 虚构身份；退款金额有优惠和分摊，不能通过单价倒推本次件数。
    raw = dict(refund_id="test-refund", tid="test-order", oid="test-child",
               num=36, refund_fee="49.28", payment="172.75", has_good_return=True,
               status="SUCCESS", sid="test-tracking")
    trade = {"orders": {"order": [dict(oid="test-child", num=36, outer_sku_id="sku#铬")]}}
    return normalize_refund({}, raw, trade)


def synced(db):
    db.get(Shop, 1).platform = "TMALL"
    repository = SqlAlchemyTmallSyncRepository(db)
    incoming = refund()
    repository.upsert_refund(1, incoming)
    db.commit()
    order = db.scalar(select(AfterSalesOrder))
    return repository, incoming, order


def test_purchase_num_is_labelled_and_persisted_on_resync(db):
    repository, incoming, order = synced(db)
    repository.upsert_refund(1, incoming)
    db.commit()
    item = order.items[0]
    assert (item.applied_quantity, item.purchased_quantity, item.quantity_source) == (
        36, 36, "TMALL_PURCHASE_NUM")


def test_confirmed_completed_partial_quantity_survives_repeated_sync(db):
    repository, incoming, order = synced(db)
    item = order.items[0]
    item.applied_quantity, item.quantity_source = 8, "TMALL_CONFIRMED_RETURN"
    db.commit()
    for _ in range(2):
        repository.upsert_refund(1, incoming)
        db.commit()
        assert (item.applied_quantity, item.purchased_quantity, item.quantity_source) == (
            8, 36, "TMALL_CONFIRMED_RETURN")


@pytest.mark.parametrize("change", [
    {"refund_amount": Decimal("50.00")}, {"return_tracking_number": "different-tracking"},
    {"platform_after_sales_status_text": "WAIT_SELLER_CONFIRM_GOODS"},
    {"platform_order_sn": "different-order"}, {"buyer_memo": "changed agreement"},
    {"after_sales_type": "ONLY_REFUND"},
    {"item": replace(refund().item, applied_quantity=40, purchased_quantity=40)},
])
def test_business_change_invalidates_confirmation_and_cannot_restore_it(db, change):
    repository, incoming, order = synced(db)
    item = order.items[0]
    item.applied_quantity, item.quantity_source = 8, "TMALL_CONFIRMED_RETURN"
    db.commit()
    repository.upsert_refund(1, replace(incoming, **change))
    db.commit()
    assert item.quantity_source == "TMALL_PURCHASE_NUM"
    repository.upsert_refund(1, incoming)
    db.commit()
    assert item.applied_quantity == 36 and item.quantity_source == "TMALL_PURCHASE_NUM"


def test_changed_sku_cannot_keep_old_confirmation(db):
    repository, incoming, order = synced(db)
    item = order.items[0]
    item.applied_quantity, item.quantity_source = 8, "TMALL_CONFIRMED_RETURN"
    db.commit()
    repository.upsert_refund(1, replace(incoming, item=replace(incoming.item, sku_code="other#铬")))
    db.commit()
    assert item.quantity_source == "TMALL_PURCHASE_NUM" and item.applied_quantity == 36


def tmall_partial(db, *, confirmed=False, task_status="SUCCEEDED"):
    db.get(Shop, 1).platform = "TMALL"
    order, receipt = fixtures.add_partial_return(db, task_status=task_status)
    item = order.items[0]
    item.purchased_quantity = 51
    item.quantity_source = "TMALL_CONFIRMED_RETURN" if confirmed else "TMALL_PURCHASE_NUM"
    if confirmed:
        item.applied_quantity = 27
    db.commit()
    return order, receipt


@pytest.mark.parametrize("confirmed", [False, True])
def test_tmall_legacy_correction_preserves_sent_todo_and_does_not_pass_quality(db, confirmed):
    order, receipt = tmall_partial(db, confirmed=confirmed)
    before = list(db.execute(select(AftersalesActionTask.__table__)).mappings())
    service = Module2ExceptionTodoService(db)
    assert service.run(include_tmall=False, dry_run=False).quantity_reviews == 0
    assert service.run(include_tmall=True, tmall_min_order_id=2).quantity_reviews == 0
    assert service.run(include_tmall=True, min_return_id=2).quantity_reviews == 0
    assert service.run(include_tmall=True, shop_codes=("excluded",)).quantity_reviews == 0
    assert service.run(include_tmall=True, dry_run=True).quantity_reviews == 1
    assert receipt.inspection_status == "FAIL"
    result = service.run(include_tmall=True, dry_run=False)
    assert result.quantity_reviews == 1 and result.tasks_created == 0
    assert receipt.inspection_status == "PENDING"
    assert "少退或未收到" not in receipt.inspection_note
    assert "少退或未收到" in receipt.note
    assert ("实收一致" in receipt.inspection_note) is confirmed
    assert list(db.execute(select(AftersalesActionTask.__table__)).mappings()) == before
    assert service.run(include_tmall=True, dry_run=False).quantity_reviews == 0


@pytest.mark.parametrize("change", ["shortage", "quality", "color"])
def test_confirmed_tmall_quantity_does_not_hide_real_failure(db, change):
    order, receipt = tmall_partial(db, confirmed=True)
    if change == "shortage":
        order.items[0].applied_quantity = 28
    elif change == "quality":
        receipt.inspected_by = "仓库质检员"
    else:
        receipt.items[0].color = "other"
    assert correct_legacy_quantity_failure(db, order, Platform.TMALL) is None
    assert receipt.inspection_status == "FAIL"


def test_publisher_blocks_old_tmall_false_shortage_without_contacting_erp(db, monkeypatch):
    db.get(Shop, 1).platform = "TMALL"
    db.commit()
    fixtures.test_publisher_cancels_queued_false_shortage_without_contacting_erp(db, monkeypatch)
