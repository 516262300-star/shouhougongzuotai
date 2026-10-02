from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask, AfterSalesOrder, MarketplaceSyncIssue, Platform,
)
from aftersales_workbench.integrations.marketplace.alibaba_1688 import normalize_1688_refund
from aftersales_workbench.integrations.marketplace.issues import SyncIssueRepository
from aftersales_workbench.integrations.marketplace.sync import MarketplaceRefundSyncService
from aftersales_workbench.services.record_status import confirmed_refund, refund_display
from aftersales_workbench.workflows.polling import utcnow
from tests import test_1688_non_financial as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


def closed_detail():
    raw = base.detail()
    raw.update(status="refundclose", extInfo={"workflowName": "cbu_return_and_refund",
                                             "refundFlowType": "V3Process"})
    return raw


def test_closed_zero_snapshot_updates_order_and_recovers_quarantine_without_tasks(db):
    repo, shop, sid = base.repo_shop(db)
    raw = closed_detail()
    previous = {**raw, "applyPayment": 2000, "status": "waitbuyermodify"}
    repo.upsert_refund(shop, sid, normalize_1688_refund(previous, base.order_detail()))
    issues = SyncIssueRepository(db)
    issues.record(sid, raw["refundId"], "缺少有效退款金额")
    issue = db.get(MarketplaceSyncIssue, (sid, raw["refundId"]))
    issue.next_retry_at = utcnow() - timedelta(seconds=1)
    repo.commit()

    class Client:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def identity(self): return shop.platform_shop_id, shop.shop_name
        def fetch_refund(self, refund_id):
            assert refund_id == raw["refundId"]
            return normalize_1688_refund(raw, base.order_detail())
        def fetch_window(self, **_): return iter(())

    service = MarketplaceRefundSyncService(repo, Settings(_env_file=None),
        client_factory=lambda _: Client(), now=lambda: 3600)
    result = service.sync_all([shop], max_windows=1)[0]
    assert result.ok and result.issues_recovered == 1
    assert issue.resolved_at is not None and issue.dismissed_at is None
    order = db.scalar(select(AfterSalesOrder))
    assert order.refund_amount == 0
    assert order.refund_financial_status == "CLOSED"
    assert order.actual_refund_amount is None and order.refund_completed_at is None
    assert not confirmed_refund(order, Platform.ALIBABA_1688)
    assert refund_display(order, Platform.ALIBABA_1688)["label"] == "退款已关闭"
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0


@pytest.mark.parametrize("status", ["refundsuccess", "waitsellerreceive", "", None])
def test_zero_amount_still_rejected_when_not_closed(status):
    with pytest.raises(ValueError):
        normalize_1688_refund({**closed_detail(), "status": status}, base.order_detail())


@pytest.mark.parametrize("field", ["applyPayment", "applyCarriage", "refundPayment", "refundCarriage"])
@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", True, -1])
def test_closed_zero_requires_all_four_explicit_zero_amounts(field, value):
    with pytest.raises(ValueError):
        normalize_1688_refund({**closed_detail(), field: value}, base.order_detail())


@pytest.mark.parametrize("field", ["refundPayment", "refundCarriage"])
def test_closed_zero_with_real_refund_conflict_stays_quarantined(field):
    with pytest.raises(ValueError):
        normalize_1688_refund({**closed_detail(), field: 100}, base.order_detail())


@pytest.mark.parametrize("change", ["order", "missing_items", "unknown_item", "duplicate_item", "counts", "quantity"])
def test_closed_zero_requires_order_and_item_identity(change):
    raw, order = closed_detail(), base.order_detail()
    if change == "order": order["baseInfo"]["idOfStr"] = "WRONG"
    elif change == "missing_items": order["productItems"] = []
    elif change == "unknown_item": raw["orderEntryCountMap"] = {"WRONG": 2}
    elif change == "duplicate_item": order["productItems"] *= 2
    elif change == "counts": raw["orderEntryCountMap"] = {}
    else: raw["orderEntryCountMap"] = {"LINE-1": 0}
    with pytest.raises(ValueError):
        normalize_1688_refund(raw, order)


def test_closed_zero_cannot_overwrite_existing_refund_success(db):
    repo, shop, sid = base.repo_shop(db)
    closed = normalize_1688_refund(closed_detail(), base.order_detail())
    paid = replace(closed, refund_amount=Decimal("20"),
        platform_after_sales_status_text="refundsuccess", refund_financial_status="SUCCESS",
        actual_refund_amount=Decimal("20"), refund_completed_at=utcnow())
    repo.upsert_refund(shop, sid, paid)
    repo.commit()
    with pytest.raises(ValueError, match="已有资金事实"):
        repo.upsert_refund(shop, sid, closed)
    repo.rollback()
    order = db.scalar(select(AfterSalesOrder))
    assert order.refund_financial_status == "SUCCESS" and order.actual_refund_amount == 20
