from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select, update
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationPollState,
    Shop,
)
from aftersales_workbench.integrations.erp.sales_owner import SalesOwnerLookup
from aftersales_workbench.integrations.pdd.client import PddApiError
from aftersales_workbench.services.historical_supplement import (
    HistoricalSupplementService,
    SupplementDataError,
    read_pdd_paid,
    verified_pdd_paid,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if isinstance(column.type, ENUM):
                column.type = String(100)
            elif isinstance(column.type, BigInteger):
                column.type = Integer()
            column.server_default = None
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add_all([
            Shop(shop_id=1, platform="PDD", shop_name="测试", shop_code="pdd-test"),
            Shop(shop_id=2, platform="TMALL", shop_name="测试", shop_code="tmall-test"),
        ])
        session.commit()
        yield session
    engine.dispose()


def add_order(db, number, **kwargs):
    values = dict(
        id=number, shop_id=1, after_sales_sn=f"af-{number}", platform_order_sn=f"order-{number}",
        after_sales_type="ONLY_REFUND", refund_amount=Decimal("1.88"),
        workflow_status="PENDING_CHECK", order_shipping_status="UNSHIPPED",
        refund_financial_status="SUCCESS", platform_order_amount=None,
        updated_at=datetime(2026, 9, 1, 10),
    )
    values.update(kwargs)
    row = AfterSalesOrder(**values)
    db.add(row)
    db.commit()
    return row


def service(db):
    return HistoricalSupplementService(db, Settings(
        _env_file=None, module2_erp_intake_min_order_id=100, tmall_module123_min_order_id=100,
    ))


def paid(_shop_code, order_sn, _after_sales_sn=None):
    return {"order_sn": order_sn, "pay_amount": "1.88", "platform_discount": "1"}


@pytest.mark.parametrize("amount", [None, True, 0, -1, "NaN", "Infinity", "abc", "1.001", "1e20"])
def test_bad_amount_never_backfilled(amount):
    with pytest.raises(SupplementDataError):
        verified_pdd_paid({"order_sn": "order", "pay_amount": amount}, "order")


def test_wrong_order_identity_rejected():
    with pytest.raises(SupplementDataError):
        verified_pdd_paid({"order_sn": "wrong", "pay_amount": "2.00"}, "order")


def test_preview_writes_nothing_then_apply_only_changes_buyer_paid(db):
    row = add_order(db, 1)
    db.add(AftersalesActionTask(
        id=1, after_sales_sn=row.after_sales_sn, action_type="ERP_CHECK_FULFILLMENT",
        action_status="PENDING", idempotency_key="task", payload={"keep": "unchanged"},
    ))
    db.commit()
    preview = service(db).run(kind="pdd_paid", max_order_id=99, read_paid=paid)
    assert preview["ready"] == 1 and preview["updated"] == 0
    assert db.query(AutomationPollState).count() == 0
    assert db.get(AfterSalesOrder, 1).platform_order_amount is None
    result = service(db).run(kind="pdd_paid", max_order_id=99, dry_run=False, read_paid=paid)
    assert result["updated"] == 1
    db.expire_all()
    row = db.get(AfterSalesOrder, 1)
    assert row.platform_order_amount == Decimal("1.88")
    assert row.merchant_receivable_amount is None and row.platform_discount_amount is None
    assert row.workflow_status == "PENDING_CHECK" and row.refund_financial_status == "SUCCESS"
    assert row.updated_at == datetime(2026, 9, 1, 10)
    task = db.get(AftersalesActionTask, 1)
    assert task.action_status == "PENDING" and task.payload == {"keep": "unchanged"}
    assert db.query(AftersalesActionTask).count() == 1
    assert service(db).run(kind="pdd_paid", max_order_id=99, read_paid=paid)["scanned"] == 0


def test_unsafe_live_records_and_non_pdd_never_selected(db):
    add_order(db, 1, order_shipping_status="IN_TRANSIT")
    add_order(db, 2, refund_financial_status="PENDING")
    add_order(db, 3, merchant_receivable_amount=Decimal("2.88"))
    add_order(db, 4, shop_id=2)
    add_order(db, 100)
    add_order(db, 6, after_sales_type="RETURN_AND_REFUND", order_shipping_status="IN_TRANSIT")
    result = service(db).run(kind="pdd_paid", max_order_id=99, dry_run=False, read_paid=paid)
    assert result["scanned"] == result["updated"] == 1
    assert db.get(AfterSalesOrder, 6).platform_order_amount == Decimal("1.88")


@pytest.mark.parametrize("kind", ["pdd_paid", "tmall_owner"])
def test_cannot_expand_automation_waterline(db, kind):
    with pytest.raises(ValueError, match="水位"):
        service(db).run(kind=kind, max_order_id=100, read_paid=paid)


def test_concurrent_sync_value_not_overwritten(db):
    add_order(db, 1)

    def concurrent(_shop, order_sn, _after_sales_sn):
        db.execute(update(AfterSalesOrder).where(AfterSalesOrder.id == 1).values(
            platform_order_amount=Decimal("3.00")
        ))
        db.commit()
        return paid(_shop, order_sn)

    result = service(db).run(kind="pdd_paid", max_order_id=99, dry_run=False, read_paid=concurrent)
    assert result["skipped_changed"] == 1 and result["updated"] == 0
    assert db.get(AfterSalesOrder, 1).platform_order_amount == Decimal("3.00")


def test_unrelated_owner_refresh_does_not_invalidate_amount_backfill(db):
    add_order(db, 1)

    def refresh_owner(_shop, order_sn, _after_sales_sn):
        db.execute(update(AfterSalesOrder).where(AfterSalesOrder.id == 1).values(
            erp_sales_owner="刚同步的业务员", erp_sales_owner_status="matched",
        ))
        db.commit()
        return paid(_shop, order_sn)

    result = service(db).run(
        kind="pdd_paid", max_order_id=99, dry_run=False, read_paid=refresh_owner,
    )
    assert result["updated"] == 1
    row = db.get(AfterSalesOrder, 1)
    assert row.erp_sales_owner == "刚同步的业务员"
    assert row.platform_order_amount == Decimal("1.88")


def test_failure_is_redacted_recorded_and_does_not_starve_following_rows(db):
    for n in range(1, 5):
        add_order(db, n)

    def failing(*_args):
        raise RuntimeError("access_token=must-not-appear")

    result = service(db).run(kind="pdd_paid", max_order_id=99, dry_run=False, read_paid=failing)
    assert result["failed"] == 3 and result["stopped_early"]
    assert "must-not-appear" not in str(result)
    second = service(db).run(kind="pdd_paid", max_order_id=99, dry_run=False, read_paid=paid)
    assert second["scanned"] == second["updated"] == 1
    assert db.get(AfterSalesOrder, 4).platform_order_amount == Decimal("1.88")
    # 点名只允许复查所列记录，且不放开平台仍待退款的记录。
    add_order(db, 5, refund_financial_status="PENDING")
    retry = service(db).run(
        kind="pdd_paid", max_order_id=99, dry_run=False, read_paid=paid, record_ids=(1, 5),
    )
    assert retry["scanned"] == retry["updated"] == 1
    assert db.get(AfterSalesOrder, 2).platform_order_amount is None
    assert db.get(AfterSalesOrder, 5).platform_order_amount is None


def test_tmall_owner_only_queries_uncached_history_without_active_tasks(db):
    add_order(db, 1, shop_id=2)
    add_order(db, 2, shop_id=2, erp_sales_owner_status="matched", erp_sales_owner="原业务员")
    add_order(db, 3, shop_id=2)
    db.add(AftersalesActionTask(
        id=1, after_sales_sn="af-3", action_type="TMALL_AGREE_REFUND",
        action_status="PENDING", idempotency_key="active",
    ))
    db.commit()
    def lookup(_):
        return SalesOwnerLookup("测试业务员", "测试客户", "matched", "")

    preview = service(db).run(kind="tmall_owner", max_order_id=99, read_owner=lookup)
    assert preview["ready"] == 1 and preview["updated"] == 0
    assert db.get(AfterSalesOrder, 1).erp_sales_owner is None
    result = service(db).run(kind="tmall_owner", max_order_id=99, dry_run=False, read_owner=lookup)
    assert result["updated"] == 1
    db.expire_all()
    row = db.get(AfterSalesOrder, 1)
    assert row.erp_sales_owner == "测试业务员"
    assert row.workflow_status == "PENDING_CHECK" and row.platform_order_amount is None
    assert db.get(AfterSalesOrder, 2).erp_sales_owner == "原业务员"
    assert db.query(AftersalesActionTask).count() == 1


def test_unmatched_history_can_be_rechecked_after_delay(db):
    add_order(db, 1, shop_id=2)
    def missing(_):
        return SalesOwnerLookup(None, None, "not_found", "")

    service(db).run(kind="tmall_owner", max_order_id=99, dry_run=False, read_owner=missing)
    assert service(db).run(kind="tmall_owner", max_order_id=99, read_owner=missing)["scanned"] == 0
    poll = db.scalar(select(AutomationPollState))
    poll.next_check_at -= timedelta(days=2)
    db.commit()
    def found(_):
        return SalesOwnerLookup("测试业务员", "测试客户", "matched", "")

    assert service(db).run(
        kind="tmall_owner", max_order_id=99, dry_run=False, read_owner=found
    )["updated"] == 1


class ArchivedOrderClient:
    def __init__(self, *, code=50001, **changes):
        self.code = code
        self.detail = {"id": 1, "order_sn": "order", "after_sales_status": 10,
                       "order_amount": 389, "refund_amount": 100}
        self.detail.update(changes)
        self.detail_calls = 0

    def get_order_information(self, **_kwargs):
        raise PddApiError(error_code=self.code, message="不可查询")

    def get_refund_information(self, **_kwargs):
        self.detail_calls += 1
        return self.detail


def test_old_order_fallback_uses_verified_order_amount_not_refund_amount():
    client = ArchivedOrderClient()
    info = read_pdd_paid(client, order_sn="order", after_sales_sn="1")
    assert verified_pdd_paid(info, "order") == Decimal("3.89")
    assert info["amount_source"] == "refund_detail"


@pytest.mark.parametrize("changes", [
    {"id": 2}, {"order_sn": "wrong"}, {"after_sales_status": 2},
    {"order_amount": None}, {"order_amount": "NaN"}, {"order_amount": 1.1},
])
def test_old_order_fallback_still_requires_exact_successful_refund(changes):
    with pytest.raises(SupplementDataError):
        read_pdd_paid(ArchivedOrderClient(**changes), order_sn="order", after_sales_sn="1")


def test_permission_and_rate_errors_do_not_trigger_fallback():
    client = ArchivedOrderClient(code=70031)
    with pytest.raises(PddApiError):
        read_pdd_paid(client, order_sn="order", after_sales_sn="1")
    assert client.detail_calls == 0
