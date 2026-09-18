from contextlib import nullcontext
from types import SimpleNamespace as NS

import pytest

from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder
from aftersales_workbench.workflows import module1_worker, windows_wecom
from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticeConfigurationError,
    DesktopNoticePlanner,
    DesktopNoticePreviewService,
)
from aftersales_workbench.workflows.desktop_sender import DesktopLedgerState, DesktopNoticeLedger
from tests import test_module2_todo_queue as fixtures


@pytest.fixture
def db():
    yield from fixtures.db.__wrapped__()


def add(db, number, *, carrier="顺丰速运", ready=True, tracking=None):
    order = AfterSalesOrder(
        id=number, shop_id=1, after_sales_sn=f"af-{number}", platform_order_sn=f"order-{number}",
        after_sales_type="ONLY_REFUND", order_shipping_status="IN_TRANSIT",
        workflow_status="PENDING_CHECK", refund_amount=10, platform_order_amount=10,
        carrier_code=carrier, forward_tracking_number=tracking or f"tracking-{number}",
    )
    task = AftersalesActionTask(
        id=number, after_sales_sn=order.after_sales_sn, action_type="QYWX_INTERCEPT_NOTIFY",
        action_status="PENDING", idempotency_key=f"test-{number}", attempts=0,
        payload={"preflight_checked_at": "2026-09-14T01:00:00", "preflight_state": "UNKNOWN",
                 "refund_gate": "HOLD"} if ready else {},
    )
    db.add_all([order, task])
    db.commit()
    return task


@pytest.mark.parametrize("alias", ["44", "顺丰速运", "顺丰快递", "顺丰", "shunfeng"])
def test_sf_alias_resolves_only_existing_whitelist(alias):
    planner = DesktopNoticePlanner({"44": "已核实顺丰群"})
    assert planner.resolve_target_group(alias) == "已核实顺丰群"
    assert DesktopNoticePlanner({"384": "其他群"}).resolve_target_group(alias) is None


def test_conflicting_alias_groups_fail_closed():
    with pytest.raises(DesktopNoticeConfigurationError, match="多个"):
        DesktopNoticePlanner({"44": "群甲", "顺丰速运": "群乙"})


def test_scan_passes_full_page_of_blocked_tasks_without_writes(db):
    for n in range(1, 106):
        add(db, n, carrier="unmapped", ready=n % 2 == 0)
    good = add(db, 106)
    result = DesktopNoticePreviewService(db, DesktopNoticePlanner({"44": "顺丰群"})).run(limit=1)
    assert result.pending_tasks == 106 and result.ready == 1
    assert result.blocked_preflight + result.blocked_missing_group == 105
    assert len(result.blocked_tasks) == 50
    assert result.plans[0].task_id == good.id and good.attempts == 0
    assert good.action_status == "PENDING"


def worker(db, tmp_path, monkeypatch):
    # 本文件测队列隔离；整包裹只读核验使用独立数据库集成测试。
    monkeypatch.setattr(module1_worker.DesktopNoticeSendService, "_package_notice_ready",
                        lambda self, plan: True)
    runtime = object.__new__(module1_worker.Module1WorkerRuntime)
    runtime.settings = NS(
        module1_desktop_send_enabled=True, module1_desktop_batch_limit=2,
        module1_desktop_lock_path=str(tmp_path/'send.lock'),
        module1_desktop_ledger_path=str(tmp_path/'ledger.jsonl'),
        module1_desktop_group_map={"44": "顺丰群"}, kuaidi100_carrier_map={},
        module1_notification_min_task_id=0, module1_desktop_process_name="WXWork.exe",
    )
    runtime.options = NS(task_limit=2)
    monkeypatch.setattr(module1_worker, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(module1_worker.Module1WorkerRuntime, "_active_pdd_shop_codes",
                        property(lambda self: ("pdd-test",)))
    calls = []
    def send(plan, hooks):
        hooks.paste_started()
        calls.append(plan)
        hooks.send_pressed()
        hooks.sent()
    monkeypatch.setattr(windows_wecom, "WindowsWeComGateway", lambda **kw: NS(send=send))
    return runtime, calls


def test_worker_sends_ready_shared_parcel_once_with_blockers(db, tmp_path, monkeypatch):
    missing = add(db, 1, carrier="unmapped")
    preflight = add(db, 2, ready=False)
    first = add(db, 3, tracking="same-package")
    second = add(db, 4, tracking="same-package")
    runtime, calls = worker(db, tmp_path, monkeypatch)
    result = runtime._process_desktop_notifications()
    assert result.status == "warning" and len(calls) == 1
    assert first.action_status == second.action_status == "SUCCEEDED"
    assert missing.action_status == preflight.action_status == "PENDING"
    assert missing.attempts == preflight.attempts == 0
    assert result.details["blocked_missing_group"] == result.details["blocked_preflight"] == 1


def test_unconfirmed_desktop_send_still_blocks_all_other_plans(db, tmp_path, monkeypatch):
    add(db, 1)
    runtime, calls = worker(db, tmp_path, monkeypatch)
    ledger = DesktopNoticeLedger(tmp_path/'ledger.jsonl')
    ledger.append(task_id=99, state=DesktopLedgerState.PASTE_STARTED, plan_hash="a" * 64)
    result = runtime._process_desktop_notifications()
    assert result.status == "failed" and calls == []
    assert result.details["blocking_task_id"] == 99
