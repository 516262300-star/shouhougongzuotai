from datetime import datetime

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AfterSalesOrder, MarketplaceSyncIssue
from aftersales_workbench.integrations.marketplace.issues import SyncIssueRepository
from aftersales_workbench.integrations.pdd.mapper import normalize_refund
from aftersales_workbench.integrations.pdd.repository import SqlAlchemyPddSyncRepository
from aftersales_workbench.workflows.sync_safety import sync_safe_order_filter
from tests import test_pdd_non_refund_sync as baseline
from tests.test_pdd_sync import _shop


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


def seed(db):
    repo = SyncIssueRepository(db)
    repo.record(1, "123", "original failure", platform_order_sn="order-1")
    row = db.get(MarketplaceSyncIssue, (1, "123"))
    row.next_retry_at = datetime(2000, 1, 1)
    db.commit()
    return repo, row


def test_dismiss_only_removes_alert_and_retry_preserving_evidence(db):
    repo, row = seed(db)
    original = (row.last_error, row.attempts, row.checked_at, row.next_retry_at)
    assert repo.due(1) == ["123"]
    assert repo.dismiss(1, "123", order_sn="order-1", reason="user requested removal")
    db.commit()
    db.expunge_all()
    row = db.get(MarketplaceSyncIssue, (1, "123"))
    assert row.dismissed_at is not None
    assert row.dismissed_reason == "user requested removal"
    assert row.resolved_at is None
    assert original == (row.last_error, row.attempts, row.checked_at, row.next_retry_at)
    assert repo.outstanding(1) == 0 and repo.due(1) == []
    assert not repo.dismiss(1, "123", order_sn="order-1", reason="repeat request")
    assert row.dismissed_reason == "user requested removal"
    repo.record(1, "123", "new duplicate error", platform_order_sn="order-1")
    assert not repo.resolve(1, "123")
    assert row.last_error == "original failure" and row.resolved_at is None


@pytest.mark.parametrize("shop_id,refund_id,order_sn,reason", [
    (2, "123", "order-1", "reason"),
    (1, "456", "order-1", "reason"),
    (1, "123", "other", "reason"),
    (1, "123", "order-1", " "),
    (1, "123", "order-1", "x" * 501),
])
def test_exact_identity_and_reason_required(db, shop_id, refund_id, order_sn, reason):
    repo, row = seed(db)
    with pytest.raises(ValueError):
        repo.dismiss(shop_id, refund_id, order_sn=order_sn, reason=reason)
    assert row.dismissed_at is None


def test_other_shop_and_other_refund_not_dismissed(db):
    repo, _ = seed(db)
    repo.record(2, "123", "other shop", platform_order_sn="order-1")
    repo.record(1, "456", "other refund", platform_order_sn="order-2")
    repo.dismiss(1, "123", order_sn="order-1", reason="user request")
    db.commit()
    assert repo.outstanding(1) == 1 and repo.outstanding(2) == 1
    assert not repo.is_dismissed(2, "123") and not repo.is_dismissed(1, "456")


def test_repeated_list_skips_dismissed_id_and_syncs_normal_record(db):
    repo, _ = seed(db)
    repo.dismiss(1, "123", order_sn="order-1", reason="user request")
    db.commit()

    class Client(baseline.DistinctMixedClient):
        def get_refund_information(self, *, order_sn, after_sales_id):
            assert after_sales_id != 123, "已忽略售后不得再次查询详情"
            return super().get_refund_information(order_sn=order_sn, after_sales_id=after_sales_id)

    pdd_repo = SqlAlchemyPddSyncRepository(db)
    result = baseline.service(pdd_repo, Client()).sync_all(
        [_shop()], statuses=(3,), max_windows=1)[0]
    assert result.ok and result.outstanding_issues == 0
    assert result.records_skipped == 1 and result.records_created == 1
    assert result.records_recovered == result.records_quarantined == 0
    assert pdd_repo.get_cursor_end(1, "refund-statuses:3") == 1800
    assert db.scalar(select(AfterSalesOrder)).after_sales_sn == "456"


def test_dismissal_does_not_unblock_existing_order_financial_actions(db):
    pdd_repo = SqlAlchemyPddSyncRepository(db)
    sid = pdd_repo.upsert_shop(_shop(), platform_shop_id="99", shop_name="test")
    refund = normalize_refund(baseline.record(3), baseline.detail(2), {
        "order_status": 2, "tracking_number": "example-tracking", "pay_amount": "1.00"})
    pdd_repo.upsert_refund(sid, refund)
    db.commit()
    repo, row = seed(db)
    repo.dismiss(sid, "123", order_sn="order-1", reason="user request")
    db.commit()
    assert row.resolved_at is None
    assert db.scalar(select(AfterSalesOrder).where(sync_safe_order_filter())) is None


def test_dismissal_can_be_rolled_back_without_deleting_row(db):
    repo, row = seed(db)
    repo.dismiss(1, "123", order_sn="order-1", reason="user request")
    db.rollback()
    assert row.dismissed_at is None and repo.outstanding(1) == 1
