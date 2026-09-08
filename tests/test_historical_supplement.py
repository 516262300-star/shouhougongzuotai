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
    verified_tmall_refund_facts,
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


@pytest.mark.parametrize("kind", [
    "pdd_paid", "tmall_owner", "tmall_status", "tmall_refund_facts",
])
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


def status_detail(_shop, order_sn, after_sales_sn):
    return {
        "tid": order_sn, "refund_id": after_sales_sn, "status": "SUCCESS",
        "order_status": "TRADE_CLOSED", "has_good_return": False,
        "refund_fee": "1.88", "payment": "0.00", "modified": "2026-09-02 10:00:00",
    }


def status_run(db, **kwargs):
    return service(db).run(
        kind="tmall_status", max_order_id=99, read_status=status_detail,
        record_ids=(1,), **kwargs,
    )


def test_tmall_status_preview_then_updates_only_three_status_fields(db):
    add_order(db, 1, shop_id=2, refund_financial_status="UNKNOWN",
              platform_order_amount=Decimal("1.88"), erp_sales_owner_status="not_found")
    assert status_run(db)["updated"] == 0
    assert db.query(AutomationPollState).count() == 0
    assert db.get(AfterSalesOrder, 1).refund_financial_status == "UNKNOWN"
    assert status_run(db, dry_run=False)["updated"] == 1
    db.expire_all()
    row = db.get(AfterSalesOrder, 1)
    assert row.refund_financial_status == row.platform_after_sales_status_text == "SUCCESS"
    assert row.platform_order_status_text == "TRADE_CLOSED"
    assert row.refund_completed_at is None  # modified 不冒充退款完成时刻。
    assert row.updated_at == datetime(2026, 9, 1, 10)
    assert row.platform_updated_at is None
    assert row.platform_order_amount == Decimal("1.88")  # 不被售后接口 payment=0 覆盖。
    assert row.merchant_receivable_amount is None
    assert row.erp_sales_owner_status == "not_found"
    assert row.workflow_status == "PENDING_CHECK" and row.order_shipping_status == "UNSHIPPED"
    assert db.query(AftersalesActionTask).count() == 0
    assert db.scalar(select(AutomationPollState)).scope == "history_tmall_status"
    assert status_run(db)["scanned"] == 0


@pytest.mark.parametrize("changes", [
    {"tid": "other"}, {"refund_id": "other"}, {"status": "CLOSED"},
    {"status": "WAIT_SELLER_AGREE"}, {"order_status": None},
    {"has_good_return": True}, {"has_good_return": None},
    {"refund_fee": None}, {"refund_fee": "NaN"}, {"refund_fee": "Infinity"},
    {"refund_fee": "0"}, {"refund_fee": "1.89"},
])
def test_tmall_status_requires_exact_identity_success_type_and_amount(db, changes):
    add_order(db, 1, shop_id=2, refund_financial_status="UNKNOWN")

    def read(*args):
        return {**status_detail(*args), **changes}

    result = service(db).run(
        kind="tmall_status", max_order_id=99, record_ids=(1,), read_status=read, dry_run=False,
    )
    assert result["failed"] == 1 and result["updated"] == 0
    assert db.get(AfterSalesOrder, 1).refund_financial_status == "UNKNOWN"
    assert db.query(AftersalesActionTask).count() == 0


def test_tmall_status_requires_explicit_ids_and_excludes_business_tasks_or_new_orders(db):
    for n, changes in enumerate([
        {"shop_id": 1}, {"refund_financial_status": "SUCCESS"},
        {"order_shipping_status": "IN_TRANSIT"}, {"workflow_status": "MANUAL_PROCESSING"},
        {"platform_after_sales_status_text": "WAIT_SELLER_AGREE"},
        {"forward_tracking_number": "tracking"}, {"after_sales_type": "RETURN_AND_REFUND"},
        {}, {}, {},
    ], start=1):
        add_order(db, n, **{"shop_id": 2, "refund_financial_status": "UNKNOWN", **changes})
    for n, task_status in [(8, "PENDING"), (9, "SUCCEEDED")]:
        db.add(AftersalesActionTask(
            id=n, after_sales_sn=f"af-{n}", action_type="TMALL_AGREE_REFUND",
            action_status=task_status, idempotency_key=f"existing-{n}",
        ))
    db.commit()
    add_order(db, 100, shop_id=2, refund_financial_status="UNKNOWN")
    with pytest.raises(ValueError, match="点名"):
        service(db).run(kind="tmall_status", max_order_id=99, read_status=status_detail)
    result = service(db).run(
        kind="tmall_status", max_order_id=99, record_ids=(*range(1, 11), 100),
        read_status=status_detail, dry_run=False,
    )
    assert result["scanned"] == result["updated"] == 1
    assert db.get(AfterSalesOrder, 10).refund_financial_status == "SUCCESS"
    assert db.get(AfterSalesOrder, 100).refund_financial_status == "UNKNOWN"
    assert db.query(AftersalesActionTask).count() == 2


def test_tmall_status_concurrent_platform_sync_is_not_overwritten(db):
    add_order(db, 1, shop_id=2, refund_financial_status="UNKNOWN")

    def concurrent(*args):
        db.execute(update(AfterSalesOrder).where(AfterSalesOrder.id == 1).values(
            refund_financial_status="CLOSED", platform_after_sales_status_text="CLOSED",
        ))
        db.commit()
        return status_detail(*args)

    result = service(db).run(
        kind="tmall_status", max_order_id=99, record_ids=(1,),
        read_status=concurrent, dry_run=False,
    )
    assert result["skipped_changed"] == 1 and result["updated"] == 0
    assert db.get(AfterSalesOrder, 1).refund_financial_status == "CLOSED"


def refund_facts(_shop, order_sn, after_sales_sn):
    return {
        "refund": {
            **status_detail(_shop, order_sn, after_sales_sn),
            "has_good_return": True, "oid": "child",
        },
        "trade": {
            "tid": order_sn, "status": "TRADE_FINISHED", "payment": "0.00",
            "consign_time": "2026-08-26 10:00:00",
            "orders": {"order": [
                {"oid": "child", "status": "TRADE_CLOSED",
                 "consign_time": "2026-08-26 10:00:00"},
                {"oid": "sibling", "status": "TRADE_FINISHED"},
            ]},
        },
    }


def add_facts_order(db, number=1, **changes):
    return add_order(db, number, **{
        "shop_id": 2, "refund_financial_status": "UNKNOWN",
        "order_shipping_status": "UNKNOWN", "after_sales_type": "RETURN_AND_REFUND",
        "platform_order_amount": Decimal("3.88"), "erp_sales_owner_status": "matched",
        **changes,
    })


def facts_run(db, **changes):
    return service(db).run(**{
        "kind": "tmall_refund_facts", "max_order_id": 99, "record_ids": (1,),
        "read_status": refund_facts, **changes,
    })


def row_values(db, number=1):
    return dict(db.execute(select(AfterSalesOrder.__table__).where(
        AfterSalesOrder.id == number,
    )).mappings().one())


def test_tmall_facts_preview_and_apply_change_only_verified_four_fields(db):
    add_facts_order(db)
    before = row_values(db)
    assert facts_run(db)["ready"] == 1
    assert row_values(db) == before
    assert db.query(AutomationPollState).count() == 0
    result = facts_run(db, dry_run=False)
    assert result["updated"] == 1 and result["failed"] == 0
    after = row_values(db)
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {
        "refund_financial_status", "platform_after_sales_status_text",
        "platform_order_status_text", "order_shipping_status",
    }
    assert after["refund_financial_status"] == "SUCCESS"
    assert after["order_shipping_status"] == "IN_TRANSIT"  # 父单完成不等于子单签收。
    assert db.query(AftersalesActionTask).count() == 0
    assert db.scalar(select(AutomationPollState)).scope == "history_tmall_refund_facts"
    assert facts_run(db)["scanned"] == 0


@pytest.mark.parametrize("changes", [
    {"tid": "wrong"}, {"refund_id": "wrong"}, {"oid": "wrong"}, {"oid": None},
    {"status": "CLOSED"}, {"status": "WAIT_SELLER_AGREE"}, {"status": "future_state"},
    {"has_good_return": False}, {"has_good_return": None}, {"has_good_return": 1},
    {"has_good_return": "true"}, {"order_status": None}, {"order_status": "new_state"},
    {"refund_fee": "1.87"}, {"refund_fee": True}, {"refund_fee": "NaN"},
    {"refund_fee": "Infinity"}, {"refund_fee": "0"}, {"refund_fee": "1.881"},
])
def test_tmall_facts_validate_refund_identity_type_status_and_amount(db, changes):
    add_facts_order(db)
    before = row_values(db)

    def read(*args):
        body = refund_facts(*args)
        body["refund"].update(changes)
        return body

    result = facts_run(db, read_status=read, dry_run=False)
    assert result["failed"] == 1 and result["updated"] == 0
    assert row_values(db) == before
    assert db.query(AftersalesActionTask).count() == 0


@pytest.mark.parametrize("changes", [
    {"tid": "wrong"}, {"orders": None}, {"orders": {"order": []}},
    {"orders": {"order": [{"oid": "child"}, {"oid": "child"}]}},
    {"orders": {"order": [None]}},
])
def test_tmall_facts_validate_trade_identity_and_unique_child(db, changes):
    add_facts_order(db)

    def read(*args):
        body = refund_facts(*args)
        body["trade"].update(changes)
        return body

    result = facts_run(db, read_status=read, dry_run=False)
    assert result["failed"] == 1 and result["updated"] == 0
    assert row_values(db)["refund_financial_status"] == "UNKNOWN"


@pytest.mark.parametrize("child_time", [None, "", "0000-00-00 00:00:00", "bad"])
def test_facts_parent_or_sibling_shipping_does_not_prove_refund_child_shipped(db, child_time):
    add_facts_order(db)

    def read(*args):
        body = refund_facts(*args)
        body["trade"]["orders"]["order"][0]["consign_time"] = child_time
        return body

    result = facts_run(db, read_status=read, dry_run=False)
    assert result["updated"] == 1 and result["outcomes"]["shipping_unchanged"] == 1
    assert row_values(db)["order_shipping_status"] == "UNKNOWN"
    assert row_values(db)["refund_financial_status"] == "SUCCESS"


def test_facts_partial_refund_is_not_reclassified_or_turned_into_intercept(db):
    add_facts_order(db, after_sales_type="ONLY_REFUND", workflow_status="PARTIAL_REFUND_EXCLUDED")

    def read(*args):
        body = refund_facts(*args)
        body["refund"]["has_good_return"] = False
        return body

    assert facts_run(db, read_status=read, dry_run=False)["updated"] == 1
    assert row_values(db)["workflow_status"] == "PARTIAL_REFUND_EXCLUDED"
    assert row_values(db)["platform_order_amount"] == Decimal("3.88")
    assert db.query(AftersalesActionTask).count() == 0


@pytest.mark.parametrize("prior,child_status,expected", [
    ("DELIVERED", "TRADE_CLOSED", "DELIVERED"),
    ("IN_TRANSIT", "TRADE_FINISHED", "DELIVERED"),
    ("UNKNOWN", "WAIT_BUYER_CONFIRM_GOODS", "IN_TRANSIT"),
])
def test_facts_preserve_shipping_and_use_exact_child_status(db, prior, child_status, expected):
    add_facts_order(db, order_shipping_status=prior)

    def read(*args):
        body = refund_facts(*args)
        body["trade"]["orders"]["order"][0]["status"] = child_status
        return body

    assert facts_run(db, read_status=read, dry_run=False)["updated"] == 1
    assert row_values(db)["order_shipping_status"] == expected


def test_facts_selection_requires_ids_and_excludes_active_completed_and_new_records(db):
    for number, changes in enumerate([
        {"shop_id": 1}, {"refund_financial_status": "SUCCESS"},
        {"platform_after_sales_status_text": "WAIT_SELLER_AGREE"},
        {"platform_order_status_text": "TRADE_CLOSED"},
        {"order_shipping_status": "UNSHIPPED"}, {"order_shipping_status": "PACKED_NOT_SHIPPED"},
        {"workflow_status": "MANUAL_PROCESSING"}, {"forward_tracking_number": "tracking"},
        {"after_sales_type": "EXCHANGE"}, {}, {}, {},
    ], start=1):
        add_facts_order(db, number, **changes)
    for number, status in [(10, "PENDING"), (11, "SUCCEEDED")]:
        db.add(AftersalesActionTask(
            id=number, after_sales_sn=f"af-{number}", action_type="TMALL_AGREE_REFUND",
            action_status=status, idempotency_key=f"task-{number}",
        ))
    db.commit()
    add_facts_order(db, 100)
    with pytest.raises(ValueError, match="点名"):
        facts_run(db, record_ids=None)
    result = facts_run(db, record_ids=(*range(1, 13), 100), dry_run=False)
    assert result["scanned"] == result["updated"] == 1
    assert row_values(db, 12)["refund_financial_status"] == "SUCCESS"
    assert row_values(db, 100)["refund_financial_status"] == "UNKNOWN"
    assert db.query(AftersalesActionTask).count() == 2


@pytest.mark.parametrize("change", ["financial", "shipping", "new_task"])
def test_facts_concurrent_changes_or_created_task_prevent_backfill(db, change):
    add_facts_order(db)

    def read(*args):
        if change == "new_task":
            db.add(AftersalesActionTask(
                id=1, after_sales_sn="af-1", action_type="TMALL_AGREE_REFUND",
                action_status="PENDING", idempotency_key="concurrent-task",
            ))
        else:
            values = (
                {"refund_financial_status": "CLOSED"} if change == "financial"
                else {"order_shipping_status": "DELIVERED"}
            )
            db.execute(update(AfterSalesOrder).where(AfterSalesOrder.id == 1).values(**values))
        db.commit()
        return refund_facts(*args)

    result = facts_run(db, read_status=read, dry_run=False)
    assert result["skipped_changed"] == 1 and result["updated"] == 0
    assert row_values(db)["platform_after_sales_status_text"] is None


def test_facts_reader_errors_redacted_and_stops_after_three(db):
    for number in range(1, 5):
        add_facts_order(db, number)

    def read(*args):
        raise RuntimeError("session=secret-must-not-appear")

    result = facts_run(db, record_ids=(1, 2, 3, 4), read_status=read, dry_run=False)
    assert result["failed"] == 3 and result["stopped_early"]
    assert "secret-must-not-appear" not in str(result)
    assert db.query(AutomationPollState).count() == 3
    assert db.query(AftersalesActionTask).count() == 0


@pytest.mark.parametrize("body", [{}, {"refund": {}, "trade": None}])
def test_facts_empty_response_is_not_success(body):
    with pytest.raises(SupplementDataError):
        verified_tmall_refund_facts(body, {})
