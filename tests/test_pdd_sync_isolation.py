from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.db.models import (
    AutomationActionType as Action,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as TaskStatus,
)
from aftersales_workbench.integrations.marketplace.issues import SyncIssueRepository
from aftersales_workbench.integrations.pdd.sync import ShopSyncResult
from aftersales_workbench.workflows.actions import ExternalActionExecutor
from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticePlanner,
    DesktopNoticePreviewService,
)
from aftersales_workbench.workflows.desktop_sender import (
    DesktopBeforePasteError,
    DesktopNoticeLedger,
    DesktopNoticeSendService,
)
from aftersales_workbench.workflows.module1 import SqlAlchemyModule1Repository
from aftersales_workbench.workflows.module1_worker import (
    Module1WorkerCycleResult,
    Module1WorkerOptions,
    Module1WorkerRuntime,
    WorkerStageResult,
)
from aftersales_workbench.workflows.sync_safety import (
    require_sync_safe_order,
    sync_safe_order_filter,
)
from tests import test_pdd_non_refund_sync as baseline
from tests.test_pdd_non_refund_sync import (
    UnknownOnlyClient,
    service,
)
from tests.test_pdd_sync import FakeRepository, _shop


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


def settings(**extra):
    return Settings(_env_file=None, pdd_app_1_client_id="id", pdd_app_1_client_secret="secret",
                    pdd_shop_1_access_token="token1", pdd_shop_4_access_token="token4", **extra)


def runtime():
    return Module1WorkerRuntime(settings(), Module1WorkerOptions(shop_numbers=(1, 4)))


def stage(*rows):
    return WorkerStageResult(status="failed", details={"shops": list(rows)})


def test_isolated_record_marks_normal_sync_complete_without_hiding_warning():
    result = service(FakeRepository(), UnknownOnlyClient()).sync_all(
        [_shop()], statuses=(3,), max_windows=1)[0]
    assert result.normal_sync_completed
    assert not result.ok
    assert result.outstanding_issues == 1


def test_fatal_sync_error_does_not_mark_normal_sync_complete():
    class Failing(FakeRepository):
        def record_issue(self, *args):
            raise RuntimeError("database unavailable")

    result = service(Failing(), UnknownOnlyClient()).sync_all(
        [_shop()], statuses=(3,), max_windows=1)[0]
    assert not result.ok
    assert not result.normal_sync_completed


def test_shop4_proceeds_when_shop1_has_quarantined_record():
    r = runtime()
    r._accept_pdd_sync_result(stage(
        {"shop_code": "pdd-shop-01", "ok": False, "normal_sync_completed": True},
        {"shop_code": "pdd-shop-04", "ok": True},
    ))
    assert r._active_pdd_shop_codes == ("pdd-shop-01", "pdd-shop-04")
    assert r._active_module12_shop_codes == r._active_pdd_shop_codes


def test_whole_shop_failure_only_holds_that_shop_and_never_uses_stale_scope():
    r = runtime()
    r._accept_pdd_sync_result(stage(
        {"shop_code": "pdd-shop-01", "ok": False},
        {"shop_code": "pdd-shop-04", "ok": True},
        {"shop_code": "pdd-shop-07", "ok": True},
    ))
    assert r._active_pdd_shop_codes == ("pdd-shop-04",)
    r._accept_pdd_sync_result(WorkerStageResult.failed(RuntimeError("sync failed")))
    assert r._active_pdd_shop_codes == ()
    assert r._active_module12_shop_codes == ()


def test_worker_warning_is_not_reported_as_whole_shop_failure(monkeypatch, db):
    from aftersales_workbench.workflows import module1_worker as worker

    rows = [ShopSyncResult(1, "pdd-shop-01", False, normal_sync_completed=True,
                          outstanding_issues=1, error="一笔历史单无法查询"),
            ShopSyncResult(4, "pdd-shop-04", True, normal_sync_completed=True)]
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "PddRefundSyncService", lambda *args: SimpleNamespace(
        sync_all=lambda *args, **kwargs: rows))
    r = runtime()
    result = r._sync()
    assert result.status == "warning"
    assert result.details["automation_shop_codes"] == ["pdd-shop-01", "pdd-shop-04"]
    summary = Module1WorkerCycleResult(started_at="test", sync=result).summary_dict()["sync"]
    assert summary["shops_warning"] == 1
    assert summary["shops_failed"] == 0


def populate(db):
    for sid in (1, 4):
        db.add(Shop(shop_id=sid, shop_code=f"pdd-shop-0{sid}", shop_name=f"test{sid}",
                    platform=Platform.PDD, is_active=1))
    for oid, sid in ((101, 1), (102, 1), (104, 4)):
        db.add(AfterSalesOrder(id=oid, shop_id=sid, platform_order_sn=f"order-{oid}",
            after_sales_sn=str(oid), after_sales_type=AfterSalesType.ONLY_REFUND,
            order_shipping_status=ShippingStatus.IN_TRANSIT,
            workflow_status=WorkflowStatus.PENDING_CHECK, refund_amount=Decimal("18.47"),
            platform_order_amount=Decimal("18.47"), platform_after_sales_status=2,
            platform_order_refund_status=2, refund_financial_status="PENDING",
            forward_tracking_number=f"JT{oid}", carrier_code="384"))
    db.commit()
    SyncIssueRepository(db).record(1, "101", "invalid details", platform_order_sn="order-101")
    db.commit()


def add_task(db, oid, kind=Action.QYWX_INTERCEPT_NOTIFY):
    task = AftersalesActionTask(after_sales_sn=str(oid), action_type=kind,
        action_status=TaskStatus.PENDING, attempts=0,
        idempotency_key=f"test:{oid}:{kind}", payload={
            "preflight_state": "IN_TRANSIT", "preflight_checked_at": datetime.now().isoformat(),
            "refund_gate": "ALLOW_AFTER_NOTICE", "origin": "module1"})
    db.add(task)
    db.commit()
    return task


def test_normal_orders_in_same_and_other_shops_continue(db):
    populate(db)
    candidates = SqlAlchemyModule1Repository(db).list_candidates(shop_codes=None, limit=20)
    assert {x.after_sales_sn for x in candidates} == {"102", "104"}
    assert db.scalars(select(AfterSalesOrder.after_sales_sn).where(
        sync_safe_order_filter(("pdd-shop-04",)))).all() == ["104"]
    assert db.scalars(select(AfterSalesOrder.id).where(sync_safe_order_filter(()))).all() == []


def test_related_refund_on_same_order_is_also_held_until_resolved(db):
    populate(db)
    # 同一个平台订单新增售后号也不能绕过其尚未查清的历史售后。
    db.get(AfterSalesOrder, 102).platform_order_sn = "order-101"
    db.commit()
    candidates = SqlAlchemyModule1Repository(db).list_candidates(shop_codes=None, limit=20)
    assert [x.after_sales_sn for x in candidates] == ["104"]
    SyncIssueRepository(db).resolve(1, "101")
    db.commit()
    assert len(SqlAlchemyModule1Repository(db).list_candidates(shop_codes=None, limit=20)) == 3


@pytest.mark.parametrize("kind", [Action.QYWX_INTERCEPT_NOTIFY, Action.PDD_AGREE_REFUND,
                                  Action.PDD_AGREE_RETURN_REFUND])
def test_old_pending_tasks_cannot_bypass_record_or_shop_guard(db, kind):
    populate(db)
    bad = add_task(db, 101, kind)
    same_shop_good = add_task(db, 102, kind)
    good = add_task(db, 104, kind)
    executor = ExternalActionExecutor(db, settings(), pdd_shop_codes=("pdd-shop-04",))
    assert [x.id for x in executor._list_pending((kind,), 20)] == [good.id]
    assert not executor._claim(bad.id)
    assert not executor._claim(same_shop_good.id)
    assert executor._claim(good.id)
    assert db.get(AftersalesActionTask, bad.id).action_status == TaskStatus.PENDING


def test_new_issue_between_listing_and_claim_prevents_money_action(db):
    populate(db)
    task = add_task(db, 104, Action.PDD_AGREE_REFUND)
    executor = ExternalActionExecutor(db, settings(), pdd_shop_codes=("pdd-shop-04",))
    assert len(executor._list_pending((Action.PDD_AGREE_REFUND,), 20)) == 1
    SyncIssueRepository(db).record(4, "104", "changed", platform_order_sn="order-104")
    db.commit()
    assert not executor._claim(task.id)
    with pytest.raises(ValueError, match="同步"):
        require_sync_safe_order(db, "104", ("pdd-shop-04",))


def test_desktop_preview_and_paste_claim_both_enforce_isolation(db, tmp_path):
    populate(db)
    bad = add_task(db, 101)
    good = add_task(db, 104)
    preview = DesktopNoticePreviewService(db, DesktopNoticePlanner({"384": "test-group"}, {}),
        pdd_shop_codes=("pdd-shop-01", "pdd-shop-04")).run(limit=20)
    assert [plan.task_id for plan in preview.plans] == [good.id]
    sender = DesktopNoticeSendService(db, None, DesktopNoticeLedger(tmp_path / "ledger.jsonl"),
        pdd_shop_codes=("pdd-shop-01", "pdd-shop-04"))
    with pytest.raises(DesktopBeforePasteError, match="同步"):
        sender._claim(bad.id)
    assert db.get(AftersalesActionTask, bad.id).action_status == TaskStatus.PENDING


def test_shared_parcel_with_isolated_task_cannot_be_partially_sent(db, tmp_path):
    populate(db)
    db.get(AfterSalesOrder, 104).forward_tracking_number = "JT101"
    db.commit()
    add_task(db, 101)
    good = add_task(db, 104)
    sender = DesktopNoticeSendService(db, None, DesktopNoticeLedger(tmp_path / "ledger.jsonl"))
    with pytest.raises(DesktopBeforePasteError, match="同步"):
        sender._claim(good.id)
