from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    MarketplaceSyncIssue,
    Shop,
)
from aftersales_workbench.integrations.pdd.client import PddApiError
from aftersales_workbench.integrations.pdd.history import (
    HISTORY_PREFIX,
    PddHistoryRepository,
    is_history,
)
from aftersales_workbench.integrations.pdd.mapper import normalize_refund
from aftersales_workbench.integrations.pdd.repository import SqlAlchemyPddSyncRepository
from aftersales_workbench.integrations.pdd.sync import PddRefundSyncService, ShopSyncResult
from aftersales_workbench.workflows.sync_safety import sync_safe_order_filter
from tests import test_pdd_non_refund_sync as baseline
from tests.test_pdd_sync import _shop

NOW = int(datetime(2026, 9, 10, tzinfo=UTC).timestamp())


@pytest.fixture(autouse=True)
def fixed_history_clock(monkeypatch):
    # 服务的now与历史仓库的utcnow必须同源，避免隔天运行时用例自动到期。
    monkeypatch.setattr(
        "aftersales_workbench.integrations.pdd.history.utcnow",
        lambda: datetime.fromtimestamp(NOW, UTC).replace(tzinfo=None),
    )


@pytest.fixture(params=[(2, 1), (3, 2)], ids=["only-refund", "return-and-refund"])
def db(request):
    for session in baseline.db.__wrapped__():
        session.info["history_type_codes"] = request.param
        yield session


class Client:
    def __init__(self):
        self.record = baseline.record(2) | {"created_time": "2025-11-17 10:24:57"}
        self.detail = baseline.detail(1) | {"recreated_at": "2025-11-17 10:24:57"}
        self.error = PddApiError(error_code=50001, message="test", sub_code="45001")
        self.detail_error = None
        self.return_list = True

    def get_refund_information(self, **kwargs):
        if self.detail_error:
            raise self.detail_error
        return self.detail

    def get_order_information(self, **kwargs):
        raise self.error

    def get_refund_list_increment(self, **kwargs):
        return {
            "refund_increment_get_response": {
                "refund_list": [self.record] if self.return_list else [],
                "total_count": 1 if self.return_list else 0,
            }
        }


def setup(db):
    repo = SqlAlchemyPddSyncRepository(db)
    sid = repo.upsert_shop(_shop(), platform_shop_id="test", shop_name="test")
    db.get(Shop, sid).created_at = datetime(2026, 8, 31)
    repo.record_issue(sid, "123", "order-1", "original 45001 evidence")
    db.commit()
    service = PddRefundSyncService(repo, Settings(_env_file=None), now=lambda: NOW)
    client = Client()
    client.record["after_sales_type"], client.detail["after_sales_type"] = (
        db.info["history_type_codes"]
    )
    return repo, sid, service, client


def save(service, sid, client):
    result = ShopSyncResult(1, "test", True)
    service._save_record(client, shop_id=sid, list_record=client.record, result=result)
    service.repository.commit()
    return result


def test_history_retains_evidence_no_current_alert_no_actions_and_survives_restart(db):
    repo, sid, svc, client = setup(db)
    row = db.get(MarketplaceSyncIssue, (sid, "123"))
    original = row.last_error, row.attempts, row.checked_at
    result = save(svc, sid, client)
    db.expunge_all()
    repo = SqlAlchemyPddSyncRepository(db)
    row = db.get(MarketplaceSyncIssue, (sid, "123"))
    assert result.records_terminal_history_skipped == 1 and result.records_quarantined == 0
    assert is_history(row) and row.resolved_at is None
    assert (row.last_error, row.attempts, row.checked_at) == original
    assert "不代表ERP平账" in row.dismissed_reason and len(row.dismissed_reason) <= 500
    assert row.next_retry_at - row.dismissed_at == timedelta(hours=24)
    assert repo.outstanding_issues(sid) == 0
    assert not repo.is_issue_dismissed(sid, "123")
    assert db.scalar(select(func.count()).select_from(AfterSalesOrder)) == 0
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0
    assert save(svc, sid, client).records_terminal_history_skipped == 1
    assert db.scalar(select(func.count()).select_from(MarketplaceSyncIssue)) == 1


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("record", "created_time", None),
        ("detail", "recreated_at", "bad-date"),
        ("record", "created_time", "2026-09-10 10:00:00"),
        ("detail", "recreated_at", "2026-09-10 10:00:00"),
        ("record", "after_sales_status", 2),
        ("detail", "after_sales_status", 3),
        ("record", "after_sales_type", 99),
        ("detail", "after_sales_type", 99),
        ("detail", "order_sn", "wrong-order"),
        ("detail", "id", 456),
        ("detail", "refund_amount", 999),
        ("record", "refund_amount", "NaN"),
    ],
)
def test_ambiguous_or_current_record_is_not_hidden(db, target, key, value):
    repo, sid, svc, client = setup(db)
    getattr(client, target)[key] = value
    result = save(svc, sid, client)
    assert result.records_terminal_history_skipped == 0
    assert repo.outstanding_issues(sid) == 1
    assert not is_history(db.get(MarketplaceSyncIssue, (sid, "123")))


def test_recent_pre_onboarding_application_is_not_history(db):
    repo, sid, svc, client = setup(db)
    client.record["created_time"] = client.detail["recreated_at"] = "2026-08-20 10:00:00"
    save(svc, sid, client)
    assert repo.outstanding_issues(sid) == 1


def test_new_state_reactivates_history_and_preserves_previous_classification(db):
    repo, sid, svc, client = setup(db)
    save(svc, sid, client)
    client.record["after_sales_status"] = client.detail["after_sales_status"] = 2
    result = save(svc, sid, client)
    row = db.get(MarketplaceSyncIssue, (sid, "123"))
    assert result.records_quarantined == 1 and repo.outstanding_issues(sid) == 1
    assert row.dismissed_at is None and row.resolved_at is None
    assert row.dismissed_reason.startswith(HISTORY_PREFIX)


@pytest.mark.parametrize("same_refund", [True, False])
def test_existing_order_or_related_refund_never_classified_history(db, same_refund):
    repo, sid, svc, client = setup(db)
    record = baseline.record(2) | {"id": 123 if same_refund else 456}
    detail = baseline.detail(1) | {"id": record["id"]}
    repo.upsert_refund(sid, normalize_refund(record, detail, {"order_status": 2}))
    db.commit()
    assert save(svc, sid, client).records_quarantined == 1
    assert repo.outstanding_issues(sid) == 1


def test_history_never_releases_financial_guard(db):
    repo, sid, svc, client = setup(db)
    save(svc, sid, client)
    repo.upsert_refund(
        sid, normalize_refund(baseline.record(2), baseline.detail(1), {"order_status": 2})
    )
    db.commit()
    assert db.scalars(select(AfterSalesOrder).where(sync_safe_order_filter())).all() == []
    assert save(svc, sid, client).records_quarantined == 1


def test_daily_retry_no_match_keeps_history_and_manual_dismiss_not_reopened(db):
    repo, sid, svc, client = setup(db)
    save(svc, sid, client)
    row = db.get(MarketplaceSyncIssue, (sid, "123"))
    assert repo.due_issues(sid) == []
    row.next_retry_at = datetime(2000, 1, 1)
    db.commit()
    assert repo.due_issues(sid) == [("123", "order-1")]
    assert repo.due_issues(sid + 1) == []
    client.return_list = False
    svc._retry_issues(client, shop_id=sid, result=ShopSyncResult(1, "test", True))
    assert is_history(row) and row.next_retry_at.year == 2026
    assert repo.outstanding_issues(sid) == 0 and row.last_error == "original 45001 evidence"
    row.dismissed_reason = "user explicitly dismissed"
    db.commit()
    assert repo.is_issue_dismissed(sid, "123")
    PddHistoryRepository(db).reopen(sid, "123")
    assert row.dismissed_at is not None


@pytest.mark.parametrize("failure", ["auth", "detail_45001"])
def test_unverified_detail_or_auth_failure_cannot_be_suppressed(db, failure):
    repo, sid, svc, client = setup(db)
    if failure == "auth":
        client.error = PddApiError(error_code=10002, message="invalid token")
        with pytest.raises(PddApiError):
            save(svc, sid, client)
        db.rollback()
    else:
        client.detail_error = client.error
        assert save(svc, sid, client).records_quarantined == 1
    assert repo.outstanding_issues(sid) == 1


def test_transaction_rollback_restores_original_issue(db):
    repo, sid, svc, client = setup(db)
    assert repo.defer_history_issue(sid, client.record, client.detail, now_at=NOW)
    db.rollback()
    assert repo.outstanding_issues(sid) == 1
    assert not is_history(db.get(MarketplaceSyncIssue, (sid, "123")))


def test_rechecking_history_after_365_days_does_not_make_dismissal_permanent(db):
    repo, sid, svc, client = setup(db)
    save(svc, sid, client)
    svc._now = lambda: NOW + 400 * 86400
    save(svc, sid, client)
    row = db.get(MarketplaceSyncIssue, (sid, "123"))
    assert is_history(row)
    assert not repo.is_issue_dismissed(sid, "123")
    assert row.next_retry_at - row.dismissed_at == timedelta(hours=24)


@pytest.mark.parametrize("offset,expected_due", [(-1, False), (0, True), (1, True)])
def test_history_daily_due_boundary_uses_consistent_clock(db, monkeypatch, offset, expected_due):
    repo, sid, svc, client = setup(db)
    save(svc, sid, client)
    at = datetime.fromtimestamp(NOW, UTC).replace(tzinfo=None) + timedelta(days=1, seconds=offset)
    monkeypatch.setattr("aftersales_workbench.integrations.pdd.history.utcnow", lambda: at)
    assert repo.due_issues(sid) == ([("123", "order-1")] if expected_due else [])


@pytest.mark.parametrize("codes", [(2, 2), (3, 1), (4, 3), (5, 4), (6, 5)])
def test_mismatched_or_non_refund_type_pairs_are_not_hidden(db, codes):
    repo, sid, svc, client = setup(db)
    client.record["after_sales_type"], client.detail["after_sales_type"] = codes
    assert save(svc, sid, client).records_quarantined == 1
    assert repo.outstanding_issues(sid) == 1
    assert not is_history(db.get(MarketplaceSyncIssue, (sid, "123")))


@pytest.mark.parametrize("status", [2, 3, 11])
def test_non_completed_status_pair_is_not_hidden(db, status):
    repo, sid, svc, client = setup(db)
    client.record["after_sales_status"] = client.detail["after_sales_status"] = status
    assert save(svc, sid, client).records_quarantined == 1
    assert repo.outstanding_issues(sid) == 1


def test_year_old_return_refund_updated_today_stays_recheckable_not_permanently_dismissed(db):
    repo, sid, svc, client = setup(db)
    client.record.update(after_sales_type=3, created_time="2025-08-07 13:11:40",
                         updated_time="2026-09-10 13:16:10")
    client.detail.update(after_sales_type=2, recreated_at="2025-08-07 13:11:40",
                         updated_time=NOW)
    result = save(svc, sid, client)
    row = db.get(MarketplaceSyncIssue, (sid, "123"))
    assert result.records_terminal_history_skipped == 1
    assert repo.outstanding_issues(sid) == 0
    assert is_history(row) and not repo.is_issue_dismissed(sid, "123")
    assert row.resolved_at is None
    assert row.next_retry_at - row.dismissed_at == timedelta(hours=24)
    assert db.scalar(select(func.count()).select_from(AfterSalesOrder)) == 0
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0
    assert save(svc, sid, client).records_terminal_history_skipped == 1
    assert db.scalar(select(func.count()).select_from(MarketplaceSyncIssue)) == 1
