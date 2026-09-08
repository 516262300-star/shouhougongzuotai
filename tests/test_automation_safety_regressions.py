from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationPollState,
    MarketplaceSyncIssue,
    Platform,
    Shop,
)
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
)
from aftersales_workbench.integrations.marketplace.models import (
    ConfiguredMarketplaceShop,
    MarketplaceRefundIssue,
    NormalizedMarketplaceItem,
    NormalizedMarketplaceRefund,
)
from aftersales_workbench.integrations.marketplace.repository import (
    SqlAlchemyMarketplaceSyncRepository,
)
from aftersales_workbench.integrations.marketplace.sync import MarketplaceRefundSyncService
from aftersales_workbench.integrations.pdd.repository import SqlAlchemyPddSyncRepository
from aftersales_workbench.workflows.module1_manual_todo import SqlAlchemyModule1ManualTodoRepository
from aftersales_workbench.workflows.module2_erp_intake import Module2ErpIntakeService
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService
from aftersales_workbench.workflows.pdd_reconciliation import PddFailedRefundReconciler
from aftersales_workbench.workflows.polling import record_poll, utcnow
from aftersales_workbench.workflows.refund_preflight import verify_pdd_refund


@pytest.fixture
def db():
    # 真实 SQL 查询/事务，独立 SQLite 内存库；不导入生产 SessionLocal。
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
        session.add(Shop(shop_id=1, platform="PDD", shop_code="pdd-1", shop_name="测试店"))
        session.commit()
        yield session
    engine.dispose()


def add_order(db, number, **kwargs):
    values = dict(
        id=number,
        shop_id=1,
        platform_order_sn=f"order-{number}",
        after_sales_sn=str(number),
        after_sales_type="ONLY_REFUND",
        refund_amount=Decimal("5.00"),
        platform_order_amount=Decimal("5.00"),
        order_shipping_status="IN_TRANSIT",
        workflow_status="MANUAL_PROCESSING",
        refund_financial_status="PENDING",
        platform_after_sales_status=2,
        forward_tracking_number=f"JT-{number}",
        erp_sales_owner="测试业务员",
        erp_sales_owner_status="matched",
    )
    values.update(kwargs)
    row = AfterSalesOrder(**values)
    db.add(row)
    return row


def test_manual_todo_second_batch_reaches_21_and_22(db):
    for n in range(1, 23):
        add_order(db, n)
    db.commit()
    repo = SqlAlchemyModule1ManualTodoRepository(db)
    first = repo.list_candidates(shop_codes=None, limit=20)
    for candidate in first:
        repo.enqueue_todo(candidate, started_at="2026-09-08 10:00:00", max_attempts=3)
    repo.commit()
    second = repo.list_candidates(shop_codes=None, limit=20)
    assert [c.after_sales_sn for c in second] == ["21", "22"]
    assert db.query(AftersalesActionTask).count() == 20


def test_module3_due_filter_runs_before_limit_and_reaches_501(db):
    for n in range(1, 502):
        add_order(
            db,
            n,
            order_shipping_status="UNSHIPPED",
            workflow_status="PENDING_CHECK",
            refund_financial_status="SUCCESS",
            platform_after_sales_status=10,
        )
        db.add(
            AftersalesActionTask(
                id=n,
                after_sales_sn=str(n),
                action_type="ERP_CHECK_FULFILLMENT",
                action_status="PENDING",
                idempotency_key=f"m3:{n}",
                payload={},
                attempts=0,
            )
        )
        if n <= 500:
            record_poll(db, scope="module3_erp", reference=str(n), delay_seconds=1800)
    db.commit()
    rows = Module3ErpRefundService(db, None)._list_candidates(limit=1, platform_order_sn=None)
    assert [order.id for _, order in rows] == [501]


def test_module2_not_found_does_not_block_later_orders_and_dry_run_is_read_only(db):
    for n in range(1, 23):
        add_order(
            db,
            n,
            after_sales_type="RETURN_AND_REFUND",
            workflow_status="PENDING_CHECK",
            return_tracking_number=f"RETURN-{n}",
            platform_after_sales_status=3,
        )
    db.commit()
    calls = []

    class Matcher:
        def lookup(self, **kwargs):
            calls.append(kwargs["platform_order_sn"])
            return ErpReturnMatchLookup(status=ErpReturnMatchStatus.NOT_FOUND, message="未到仓")

    service = Module2ErpIntakeService(db, Matcher())
    service.run(limit=20, dry_run=True)
    assert db.query(AutomationPollState).count() == 0
    calls.clear()
    service.run(limit=20, dry_run=False)
    assert len(calls) == 20
    service.run(limit=20, dry_run=False)
    assert calls[-2:] == ["order-21", "order-22"]
    assert db.query(AutomationPollState).count() == 22


def test_module2_shared_tracking_is_detected_outside_current_page(db):
    add_order(
        db,
        1,
        after_sales_type="RETURN_AND_REFUND",
        workflow_status="PENDING_CHECK",
        return_tracking_number="SHARED",
        platform_after_sales_status=3,
    )
    add_order(
        db,
        2,
        after_sales_type="RETURN_AND_REFUND",
        workflow_status="RETURN_INSPECTED_PASS",
        return_tracking_number="SHARED",
        platform_after_sales_status=10,
    )
    db.commit()
    service = Module2ErpIntakeService(db, None)  # 若错误调用 matcher，测试必须失败。
    result = service.run(limit=1, dry_run=False)
    assert result.ambiguous == 1
    assert result.receipts_created == 0
    assert result.unavailable == 0


class GuardClient:
    def __init__(self):
        self.detail = dict(
            id=123,
            order_sn="order-1",
            after_sales_status=2,
            after_sales_type=1,
            refund_amount=188,
            order_amount=188,
            out_sku_sn="SKU",
            goods_number=1,
        )
        self.info = dict(
            order_sn="order-1",
            order_status=2,
            tracking_number="JT-1",
            pay_amount="1.88",
            platform_discount="1.00",
            refund_status=1,
        )

    def get_refund_information(self, **_kwargs):
        return self.detail

    def get_order_information(self, **_kwargs):
        return {"order_info_get_response": {"order_info": self.info}}


def guard_order():
    return SimpleNamespace(
        platform_order_sn="order-1",
        after_sales_sn="123",
        after_sales_type="ONLY_REFUND",
        refund_amount=Decimal("1.88"),
        forward_tracking_number="JT-1",
        items=[SimpleNamespace(sku_code="SKU", applied_quantity=1)],
    )


def test_refund_guard_accepts_buyer_full_refund_despite_platform_coupon():
    assert verify_pdd_refund(GuardClient(), guard_order(), origin="module1") is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("after_sales_status", 11),
        ("after_sales_status", None),
        ("id", 999),
        ("order_sn", "other"),
        ("refund_amount", 100),
        ("order_amount", 500),
        ("goods_number", 2),
        ("after_sales_type", 2),
        ("out_sku_sn", "DIFFERENT"),
        ("refund_amount", "NaN"),
    ],
)
def test_refund_guard_blocks_changed_or_missing_facts(field, value):
    client = GuardClient()
    client.detail[field] = value
    with pytest.raises(ValueError):
        verify_pdd_refund(client, guard_order(), origin="module1")


@pytest.mark.parametrize("field,value", [("order_status", 3), ("tracking_number", "JT-2")])
def test_refund_guard_blocks_new_delivery_or_tracking(field, value):
    client = GuardClient()
    client.info[field] = value
    with pytest.raises(ValueError):
        verify_pdd_refund(client, guard_order(), origin="module1")


def test_refund_guard_recognizes_explicit_success_without_write():
    client = GuardClient()
    client.detail["after_sales_status"] = 10
    assert verify_pdd_refund(client, guard_order(), origin="module1") is True


def normalized(refund_id):
    return NormalizedMarketplaceRefund(
        after_sales_sn=refund_id,
        platform_order_sn=f"trade-{refund_id}",
        after_sales_type="ONLY_REFUND",
        refund_amount=Decimal("5"),
        platform_order_amount=Decimal("5"),
        platform_goods_amount=Decimal("5"),
        buyer_reason_raw=None,
        buyer_memo=None,
        product_name=None,
        platform_created_at=None,
        platform_updated_at=None,
        forward_tracking_number=None,
        return_tracking_number=None,
        carrier_code=None,
        order_shipping_status="UNSHIPPED",
        platform_after_sales_status_text="refundsuccess",
        platform_order_status_text=None,
        items=(NormalizedMarketplaceItem("SKU", 1),),
    )


def test_sync_quarantines_invalid_record_advances_and_recovers_later(db):
    config = ConfiguredMarketplaceShop(
        platform=Platform.ALIBABA_1688,
        shop_number=1,
        shop_code="1688-1",
        shop_name="测试1688",
        platform_shop_id="1688-1",
        app_key=SecretStr("test"),
        app_secret=SecretStr("test"),
    )

    class Client:
        first = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def identity(self):
            return ("1688-1", "测试1688")

        def fetch_refund(self, refund_id):
            return normalized(refund_id)

        def fetch_window(self, **_kwargs):
            if self.first:
                yield normalized("GOOD-1")
                yield MarketplaceRefundIssue("BAD", "缺少有效退款金额")
                yield normalized("GOOD-2")

    client = Client()
    repo = SqlAlchemyMarketplaceSyncRepository(db)
    service = MarketplaceRefundSyncService(
        repo,
        Settings(
            _env_file=None,
            marketplace_sync_initial_lookback_hours=1,
            marketplace_sync_window_hours=1,
        ),
        client_factory=lambda _shop: client,
        now=lambda: 3600,
    )
    result = service.sync_all([config], max_windows=1)[0]
    assert result.records_created == 2
    assert result.records_quarantined == 1
    assert result.ok is False  # 同步推进，但不能掩盖异常单。
    issue = db.scalar(select(MarketplaceSyncIssue))
    assert repo.get_cursor_end(issue.shop_id, "refunds:1688") == 3600
    assert db.scalar(select(AfterSalesOrder).where(AfterSalesOrder.after_sales_sn == "BAD")) is None
    issue.next_retry_at = utcnow() - timedelta(seconds=1)
    db.commit()
    client.first = False
    service._now = lambda: 7200
    recovered = service.sync_all([config], max_windows=1)[0]
    assert recovered.issues_recovered == 1
    assert recovered.ok is True
    assert issue.resolved_at is not None
    assert (
        db.scalar(select(AfterSalesOrder).where(AfterSalesOrder.after_sales_sn == "BAD"))
        is not None
    )


@pytest.mark.parametrize("status,expected", [(11, "UNKNOWN"), (2, "PENDING"), (10, "SUCCESS")])
def test_failed_refund_reconciliation_never_requeues_write(db, status, expected):
    order = add_order(db, 1, workflow_status="INTERCEPT_CONFIRMED")
    task = AftersalesActionTask(
        after_sales_sn="1",
        action_type="PDD_AGREE_REFUND",
        action_status="FAILED",
        idempotency_key="failed:1",
        payload={"origin": "module1"},
        attempts=1,
        last_error="写入结果不明",
    )
    db.add(task)
    db.commit()
    PddFailedRefundReconciler(db, None).apply_observation(task, order, status, Decimal("5"))
    db.commit()
    assert order.refund_financial_status == expected
    assert task.action_status != "PENDING"
    assert task.payload["original_execution_error"] == "写入结果不明"
    assert task.attempts == 1
    if status == 10:
        assert task.action_status == "SUCCEEDED"
        assert order.actual_refund_amount == Decimal("5")
        assert order.refund_completed_at is None
        assert order.workflow_status == "RETURN_WAITING_ERP_MATCH"
    else:
        assert task.action_status == "FAILED"
        assert order.workflow_status == "MANUAL_PROCESSING"


def test_pdd_repository_cannot_overwrite_another_platform_order(db):
    order = add_order(db, 1)
    db.add(Shop(shop_id=2, platform="TMALL", shop_code="tmall-2", shop_name="测试天猫"))
    order.shop_id = 2
    db.commit()
    with pytest.raises(ValueError, match="其他平台或店铺"):
        SqlAlchemyPddSyncRepository(db).upsert_refund(1, SimpleNamespace(after_sales_sn="1"))
    assert order.shop_id == 2


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_marketplace_invalid_money_is_a_quarantinable_validation_error(value):
    from aftersales_workbench.integrations.marketplace.mapping import money

    with pytest.raises(ValueError):
        money(value, field="refund_amount")
