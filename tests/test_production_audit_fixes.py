from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    MoneyOperation,
    PlatformSyncCursor,
    Shop,
    TmallSyncCursor,
)
from aftersales_workbench.integrations.marketplace.alibaba_1688 import Alibaba1688ReadClient
from aftersales_workbench.integrations.marketplace.jd import JdReadClient
from aftersales_workbench.integrations.marketplace.models import MarketplaceApiError
from aftersales_workbench.integrations.marketplace.pagination import PageGuard
from aftersales_workbench.services.refund_confirmation_view import money_confirmation_summary
from aftersales_workbench.services.sync_freshness import sync_freshness
from aftersales_workbench.workflows import tmall_money_confirmation as confirmation
from aftersales_workbench.workflows.module3_exception_todo import (
    Module3ExceptionTodoService,
    SqlAlchemyModule3ExceptionTodoRepository,
)
from aftersales_workbench.workflows.money_operations import operation_key, run_money_write
from tests.test_pdd_non_refund_sync import db as base_db


@pytest.fixture
def db():
    yield from base_db.__wrapped__()


@pytest.mark.parametrize(
    "unavailable,not_configured,expected", [(0, 0, "completed"), (1, 0, "failed"), (0, 1, "failed")]
)
def test_owner_technical_failure_reaches_stage(monkeypatch, unavailable, not_configured, expected):
    from aftersales_workbench.workflows import module1_worker as m

    runtime = object.__new__(m.Module1WorkerRuntime)
    runtime.settings = Settings(_env_file=None, erp_sales_owner_sync_enabled=True)
    metrics = dict(unavailable=unavailable, not_configured=not_configured, not_found=3)
    monkeypatch.setattr(m, "SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(m, "get_erp_sales_owner_resolver", lambda: None)
    monkeypatch.setattr(
        m,
        "ErpSalesOwnerSyncService",
        lambda *a: SimpleNamespace(
            sync_stale=lambda **kw: SimpleNamespace(safe_dict=lambda: metrics)
        ),
    )
    result = runtime._sync_sales_owners()
    assert result.status == expected
    assert result.details == metrics


def add_order(db, index, platform="PDD", shop_id=1):
    order = AfterSalesOrder(
        shop_id=shop_id,
        platform_order_sn=str(8000 + index),
        after_sales_sn=str(9000 + index),
        after_sales_type="ONLY_REFUND",
        refund_amount=Decimal("1.00"),
        order_shipping_status="UNSHIPPED",
        workflow_status="PENDING_CHECK",
        erp_sales_owner="原销售",
        erp_sales_owner_status="matched",
    )
    db.add(order)
    db.flush()
    return order


def test_todo_limit_after_qualification_and_cooldown_survives_new_repository(db):
    db.add(Shop(shop_id=1, platform="PDD", shop_code="pdd-shop-01", shop_name="隔离测试"))
    for i in range(502):
        o = add_order(db, i)
        db.add(
            AftersalesActionTask(
                after_sales_sn=o.after_sales_sn,
                action_type="ERP_CHECK_FULFILLMENT",
                action_status="PENDING",
                idempotency_key=f"source:{i}",
                payload={
                    "origin": "module3",
                    "erp_refund_status": "blocked" if i in [0, 501] else "not_required",
                },
            )
        )
    db.flush()
    first = db.scalar(select(AfterSalesOrder).where(AfterSalesOrder.after_sales_sn == "9000"))
    first.erp_sales_owner = None
    first.erp_sales_owner_status = "not_found"
    db.add(
        AftersalesActionTask(
            after_sales_sn="9000",
            action_type="ERP_CREATE_MANUAL_TODO",
            action_status="PENDING",
            idempotency_key="existing",
            payload={"origin": "module3"},
        )
    )
    db.commit()

    def service():
        return Module3ExceptionTodoService(SqlAlchemyModule3ExceptionTodoRepository(db))

    assert service().run(limit=1, dry_run=False).tasks_existing == 1
    assert service().run(limit=1, dry_run=False).tasks_created == 1
    assert service().run(limit=1, dry_run=False).scanned == 0
    assert (
        db.scalar(
            select(func.count())
            .select_from(AftersalesActionTask)
            .where(
                AftersalesActionTask.after_sales_sn == "9501",
                AftersalesActionTask.action_type == "ERP_CREATE_MANUAL_TODO",
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"jingdong_pop_afs_soa_refundapply_queryPageList_responce": {"code": 403}},
        {
            "jingdong_pop_afs_soa_refundapply_queryPageList_responce": {
                "queryResult": {"code": 403, "result": []}
            }
        },
    ],
)
def test_jd_error_response_never_becomes_empty_success(body):
    client = object.__new__(JdReadClient)
    client.execute_read = lambda *a: body
    with pytest.raises(MarketplaceApiError):
        list(client.fetch_window(start_modified_at=1, end_modified_at=100, page_size=50))


@pytest.mark.parametrize(
    "body",
    [
        {"result": {"totalCount": 100}},
        {"result": {"opOrderRefundModels": None}},
        {"success": False, "result": {"opOrderRefundModels": []}},
    ],
)
def test_1688_error_response_never_becomes_empty_success(body):
    client = object.__new__(Alibaba1688ReadClient)
    client.get_refunds = lambda **kw: body
    with pytest.raises(MarketplaceApiError):
        list(client.fetch_window(start_modified_at=1, end_modified_at=100, page_size=20))


def test_partial_repeated_and_legitimate_empty_pages():
    guard = PageGuard(label="test", page_size=2)
    with pytest.raises(MarketplaceApiError):
        guard.read({"total": 3, "rows": [{"id": 1}]}, "rows")
    guard = PageGuard(label="test", page_size=2)
    assert guard.read({"rows": [{"id": 1}, {"id": 2}]}, "rows")[1] is False
    with pytest.raises(MarketplaceApiError):
        guard.read({"rows": [{"id": 1}, {"id": 2}]}, "rows")
    assert PageGuard(label="test", page_size=2).read({"total": 0}, "rows") == ([], True)


def test_watermarks_are_shop_scoped_and_unrelated_order_updates_do_not_change_them(db):
    db.add_all(
        [
            Shop(shop_id=1, platform="TMALL", shop_code="tmall-shop-01", shop_name="测试"),
            Shop(shop_id=2, platform="JD", shop_code="jd-relay-01", shop_name="测试2"),
        ]
    )
    db.flush()
    db.add(
        TmallSyncCursor(
            shop_id=1,
            sync_scope="refunds:all",
            cursor_end_at=1,
            last_success_at=datetime(2026, 9, 30, 1),
        )
    )
    db.add(
        PlatformSyncCursor(
            shop_id=2,
            sync_scope="refunds:jd",
            cursor_end_at=1,
            last_success_at=datetime(2026, 9, 29, 1),
        )
    )
    o = add_order(db, 0, platform="TMALL")
    db.commit()
    before = sync_freshness(db, platform="JD")
    o.updated_at = datetime(2026, 9, 30, 20)
    db.commit()
    assert sync_freshness(db, platform="JD")["oldest_success_at"] == before["oldest_success_at"]
    assert before["oldest_success_at"] == "2026-09-29T01:00:00+00:00"
    assert sync_freshness(db, shop_id=1)["oldest_success_at"] == "2026-09-30T01:00:00+00:00"


def money_fixture(db, monkeypatch):
    db.add(
        Shop(
            shop_id=1,
            platform="TMALL",
            shop_code="tmall-shop-01",
            shop_name="测试",
            platform_shop_id="seller-1",
        )
    )
    o = add_order(db, 1, platform="TMALL")
    db.add(
        AfterSalesItem(
            after_sales_sn=o.after_sales_sn,
            sku_code="SKU#BLUE",
            color="BLUE",
            applied_quantity=1,
            inspected_quantity=0,
            item_status="NORMAL",
        )
    )
    task = AftersalesActionTask(
        after_sales_sn=o.after_sales_sn,
        action_type="TMALL_AGREE_REFUND",
        action_status="SUCCEEDED",
        attempts=1,
        idempotency_key="only-task",
        payload={"origin": "module1"},
    )
    db.add(task)
    db.flush()
    op = MoneyOperation(
        operation_key=operation_key("TMALL", 1, o.after_sales_sn, "PLATFORM_REFUND"),
        platform="TMALL",
        shop_id=1,
        after_sales_sn=o.after_sales_sn,
        operation_type="PLATFORM_REFUND",
        task_id=task.id,
        state="ACKNOWLEDGED",
        started_at=datetime(2026, 9, 1),
        updated_at=datetime(2026, 9, 1),
        snapshot={
            "platform_order_sn": o.platform_order_sn,
            "refund_amount": "1.00",
            "items": [{"sku": "SKU#BLUE", "color": "BLUE", "quantity": 1}],
        },
    )
    db.add(op)
    db.commit()

    class ReadClient:
        def __init__(self):
            self.detail = {
                "refund_id": o.after_sales_sn,
                "tid": o.platform_order_sn,
                "oid": "7001",
                "refund_fee": "1.00",
                "has_good_return": False,
                "num": 1,
                "status": "SUCCESS",
            }
            self.seller = "seller-1"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get_seller(self):
            return {"user_seller_get_response": {"user": {"user_id": self.seller}}}

        def get_refund(self, **kw):
            return {"refund_get_response": {"refund": self.detail}}

        def get_trade_fullinfo(self, **kw):
            return {
                "trade_fullinfo_get_response": {
                    "trade": {
                        "tid": o.platform_order_sn,
                        "orders": {
                            "order": [{"oid": "7001", "outer_sku_id": "SKU#BLUE", "num": 1}]
                        },
                    }
                }
            }

        def agree_refund(self, **kw):
            pytest.fail("只读回查禁止退款")

    client = ReadClient()
    monkeypatch.setattr(
        confirmation,
        "load_configured_tmall_shops",
        lambda *a, **kw: [SimpleNamespace(shop_code="tmall-shop-01")],
    )
    service = confirmation.TmallMoneyConfirmation(
        db, Settings(_env_file=None), client_factory=lambda _: client
    )
    return o, task, op, client, service


def test_tmall_only_confirms_original_ledger_and_never_reopens_money(db, monkeypatch):
    o, task, op, client, service = money_fixture(db, monkeypatch)
    assert service.run(dry_run=True)["confirmed"] == 1 and op.state == "ACKNOWLEDGED"
    assert service.run(dry_run=False)["confirmed"] == 1 and op.state == "CONFIRMED"
    assert task.action_status == "SUCCEEDED" and task.attempts == 1
    assert o.workflow_status == "PENDING_CHECK"
    assert op.snapshot["readonly_confirmation"]["previous_state"] == "ACKNOWLEDGED"
    assert service.run(dry_run=False)["scanned"] == 0
    with pytest.raises(ValueError, match="已经发起"):
        run_money_write(
            db,
            o,
            operation_type="PLATFORM_REFUND",
            task_id=task.id,
            write=lambda: pytest.fail("不可重发"),
        )


@pytest.mark.parametrize(
    "change",
    [
        "shop",
        "id",
        "amount",
        "quantity",
        "missing_quantity",
        "zero_quantity",
        "snapshot",
        "timeout",
        "pending",
    ],
)
def test_tmall_uncertain_or_inconsistent_evidence_never_confirms(db, monkeypatch, change):
    o, task, op, client, service = money_fixture(db, monkeypatch)
    if change == "shop":
        client.seller = "other"
    elif change == "id":
        client.detail["refund_id"] = "other"
    elif change == "amount":
        client.detail["refund_fee"] = "2"
    elif change == "quantity":
        client.detail["num"] = 2
    elif change == "missing_quantity":
        client.detail.pop("num")
    elif change == "zero_quantity":
        client.detail["num"] = 0
    elif change == "snapshot":
        op.snapshot = {"platform_order_sn": o.platform_order_sn}
        db.commit()
    elif change == "pending":
        client.detail["status"] = "WAIT_SELLER_AGREE"
    else:

        def fail(**kw):
            raise TimeoutError("mock timeout")

        client.get_refund = fail
    result = service.run(dry_run=False)
    assert result["confirmed"] == 0 and op.state == "ACKNOWLEDGED"
    assert task.attempts == 1


@pytest.mark.parametrize("change", ["money", "task", "order", "items"])
def test_tmall_readback_does_not_overwrite_concurrent_changes(db, monkeypatch, change):
    o, task, op, client, service = money_fixture(db, monkeypatch)
    original = client.get_trade_fullinfo

    def changed(**kw):
        result = original(**kw)
        if change == "money":
            statement = (
                update(MoneyOperation)
                .where(MoneyOperation.operation_key == op.operation_key)
                .values(state="CONFIRMED")
            )
        elif change == "task":
            statement = (
                update(AftersalesActionTask)
                .where(AftersalesActionTask.id == task.id)
                .values(action_status="RUNNING")
            )
        elif change == "items":
            statement = (
                update(AfterSalesItem)
                .where(AfterSalesItem.after_sales_sn == o.after_sales_sn)
                .values(applied_quantity=2)
            )
        else:
            statement = (
                update(AfterSalesOrder)
                .where(AfterSalesOrder.after_sales_sn == o.after_sales_sn)
                .values(refund_amount=Decimal("2.00"))
            )
        db.execute(statement.execution_options(synchronize_session=False))
        db.commit()
        return result

    client.get_trade_fullinfo = changed
    result = service.run(dry_run=False)
    assert result["confirmed"] == 0 and result["pending"] == 1
    assert "readonly_confirmation" not in op.snapshot
    assert op.state == ("CONFIRMED" if change == "money" else "ACKNOWLEDGED")


def test_tmall_failed_commit_leaves_original_ledger_unconfirmed(db, monkeypatch):
    o, task, op, client, service = money_fixture(db, monkeypatch)
    original = db.commit
    failed = False

    def fail_once():
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("isolated failed commit")
        original()

    monkeypatch.setattr(db, "commit", fail_once)
    result = service.run(dry_run=False)
    assert result["unavailable"] == 1 and result["confirmed"] == 0
    assert op.state == "ACKNOWLEDGED" and "readonly_confirmation" not in op.snapshot


def test_cross_platform_monitor_counts_orphan_unknown_and_keeps_legacy_separate(db, monkeypatch):
    o, task, op, client, service = money_fixture(db, monkeypatch)
    for i, legacy in [(2, False), (3, True)]:
        other = add_order(db, i)
        db.add(
            MoneyOperation(
                operation_key=f"orphan-{i}",
                platform="TAOBAO",
                shop_id=1,
                after_sales_sn=other.after_sales_sn,
                operation_type="PLATFORM_REFUND",
                state="UNKNOWN",
                task_id=None,
                started_at=datetime(2026, 9, 1),
                updated_at=datetime(2026, 9, 1),
                snapshot=None,
                last_error="升级前资金任务待核验" if legacy else "结果未知",
            )
        )
    db.commit()
    result = money_confirmation_summary(db)
    assert result["pending"] == 2 and result["legacy_protected"] == 1


def test_bad_window_does_not_commit_sync_cursor():
    from aftersales_workbench.db.models import Platform
    from aftersales_workbench.integrations.marketplace.sync import MarketplaceRefundSyncService
    from tests.test_marketplace_clients import _shop
    from tests.test_marketplace_sync import FakeRepository

    client = object.__new__(JdReadClient)
    client.identity = lambda: ("seller-1", "测试")
    client.execute_read = lambda *a: {}
    repo = FakeRepository()
    result = MarketplaceRefundSyncService(
        repo,
        Settings(_env_file=None, marketplace_sync_initial_lookback_hours=1),
        client_factory=lambda _: nullcontext(client),
        now=lambda: 3600,
    ).sync_all([_shop(Platform.JD)], max_windows=1)[0]
    assert not result.ok and repo.cursor is None and repo.rollbacks == 1


def test_owner_next_retry_and_success_time_are_independent(db):
    from aftersales_workbench.integrations.erp.sales_owner import (
        ErpSalesOwnerSyncService,
        SalesOwnerLookup,
    )

    db.add(Shop(shop_id=1, platform="PDD", shop_code="pdd-shop-01", shop_name="测试"))
    order = add_order(db, 0)
    original_success = datetime(2026, 1, 1)
    order.erp_sales_owner_last_success_at = original_success
    order.erp_sales_owner_synced_at = datetime(2026, 1, 1)
    order.erp_sales_owner_next_retry_at = datetime.now() + timedelta(hours=1)
    db.commit()
    resolver = SimpleNamespace(
        resolve_many=lambda sns: {
            sn: SalesOwnerLookup(None, None, "unavailable", "mock timeout") for sn in sns
        }
    )
    service = ErpSalesOwnerSyncService(db, resolver)
    assert service.sync_stale(limit=1, refresh_seconds=86400).scanned == 0
    order.erp_sales_owner_next_retry_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    before = datetime.now()
    assert service.sync_stale(limit=1, refresh_seconds=86400).unavailable == 1
    assert order.erp_sales_owner_checked_at >= before
    assert order.erp_sales_owner_last_success_at == original_success
    assert service.sync_stale(limit=1, refresh_seconds=86400).scanned == 0


def test_log_archive_backup_contains_both_runs(tmp_path):
    import importlib.util
    import zipfile
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "audit_snapshot", Path(__file__).parents[1] / "scripts/office_state_snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "isolated-app"
    for run in ["run-a", "run-b"]:
        p = root / ".runtime/worker-log-history" / run / "module1-worker.log"
        p.parent.mkdir(parents=True)
        p.write_text(run)
    output = tmp_path / "backup"
    output.mkdir()
    module.archive_worker_history(root, output)
    with zipfile.ZipFile(output / "worker-log-history.zip") as archive:
        assert len(archive.namelist()) == 2
        assert archive.read("run-a/module1-worker.log") == b"run-a"
        assert archive.testzip() is None


def test_owner_time_migration_preserves_unknown_history():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = (
        Path(__file__).parents[1] / "migrations/versions/20260930_0030_sales_owner_attempt_times.py"
    )
    spec = importlib.util.spec_from_file_location("owner_time_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE aftersales_orders (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO aftersales_orders (id) VALUES (1)"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        columns = inspect(connection).get_columns("aftersales_orders")
        assert len(columns) == 4 and all(c["nullable"] for c in columns if c["name"] != "id")
        row = connection.execute(text("SELECT * FROM aftersales_orders")).one()
        assert tuple(row) == (1, None, None, None)
        with pytest.raises(RuntimeError, match="保留"):
            module.downgrade()
    engine.dispose()
