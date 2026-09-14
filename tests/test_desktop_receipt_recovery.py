from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder
from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticeCandidate,
    DesktopNoticePlanner,
)
from aftersales_workbench.workflows.desktop_receipt_recovery import recover_send_pressed
from aftersales_workbench.workflows.desktop_sender import (
    DesktopLedgerState,
    DesktopNoticeLedger,
    desktop_notice_plan_hash,
)
from aftersales_workbench.workflows.parcel_notice_store import ParcelNoticeStore
from tests import test_module2_todo_queue as sqlite_base


@pytest.fixture
def db():
    yield from sqlite_base.db.__wrapped__()


@pytest.fixture
def receipt_case(db, tmp_path):
    order = AfterSalesOrder(
        id=7, shop_id=1, after_sales_sn='test-af', platform_order_sn='test-order',
        after_sales_type='ONLY_REFUND', order_shipping_status='IN_TRANSIT',
        workflow_status='PENDING_CHECK', refund_amount=10,
        forward_tracking_number='JT-TEST', carrier_code='384',
    )
    task = AftersalesActionTask(
        id=77, after_sales_sn=order.after_sales_sn, action_type='QYWX_INTERCEPT_NOTIFY',
        action_status='RUNNING', idempotency_key='test-notice', attempts=1, payload={},
    )
    db.add_all([order, task])
    db.commit()
    planner = DesktopNoticePlanner({'384': '测试快递群'})
    plan = planner.build(DesktopNoticeCandidate(
        77, 'test-af', 'test-order', '', 'JT-TEST', '384'))
    plan_hash = desktop_notice_plan_hash(plan)
    ledger = DesktopNoticeLedger(tmp_path / 'ledger.jsonl')
    ledger.append(task_id=77, state=DesktopLedgerState.SEND_PRESSED, plan_hash=plan_hash)
    store = ParcelNoticeStore(db)
    store.claim(plan, plan_hash)
    store.update(plan, 'SendPressed')
    calls = []
    # 测试网关故意不提供 send 方法，任何重发都会失败。
    gateway = NS(verify_existing_receipt=lambda p: calls.append(p) or {'verified': True})
    return NS(db=db, order=order, task=task, planner=planner, plan=plan, ledger=ledger,
              store=store, calls=calls, gateway=gateway, now=datetime(2026, 9, 14, tzinfo=UTC))


def recover(c, **kw):
    return recover_send_pressed(c.db, c.ledger, c.planner, lambda: c.gateway, now=c.now, **kw)


def test_verified_original_message_completes_once_without_resending(receipt_case):
    c = receipt_case
    assert recover(c)
    assert c.ledger.latest(77).state == 'Sent'
    assert c.store.get(c.plan).state == 'Sent'
    assert c.task.action_status == 'SUCCEEDED'
    assert c.task.attempts == 1 and len(c.calls) == 1
    assert c.order.workflow_status == 'INTERCEPT_PUSHED'
    assert len(c.db.scalars(select(AftersalesActionTask)).all()) == 1
    assert not recover(c) and len(c.calls) == 1


@pytest.mark.parametrize('change', ['group', 'tracking', 'hash', 'missing_parcel', 'cancelled'])
def test_changed_identity_never_reads_or_confirms(receipt_case, change):
    c = receipt_case
    if change == 'group':
        c.planner = DesktopNoticePlanner({'384': '另一个群'})
    elif change == 'tracking':
        c.order.forward_tracking_number = 'OTHER'
    elif change == 'hash':
        c.store.get(c.plan).plan_hash = 'wrong'
    elif change == 'missing_parcel':
        c.db.delete(c.store.get(c.plan))
    else:
        c.task.action_status = 'CANCELLED'
    c.db.commit()
    assert not recover(c) and c.calls == []
    assert c.ledger.latest(77).state == 'SendPressed'


def test_draft_phase_is_never_auto_recovered(receipt_case):
    c = receipt_case
    c.ledger.append(task_id=77, state=DesktopLedgerState.PASTE_STARTED,
                    plan_hash=desktop_notice_plan_hash(c.plan))
    assert not recover(c) and not c.calls


@pytest.mark.parametrize('error', ['用户按下 ESC', '检测到安全验证窗口'])
def test_explicit_stop_is_not_auto_recovered(receipt_case, error):
    c = receipt_case
    c.ledger.append(task_id=77, state=DesktopLedgerState.SEND_PRESSED,
                    plan_hash=desktop_notice_plan_hash(c.plan), error=error)
    assert not recover(c) and not c.calls


def test_failure_is_rate_limited_and_later_valid_receipt_self_recovers(receipt_case):
    c = receipt_case
    c.gateway.verify_existing_receipt = lambda p: c.calls.append(p) or {'verified': False}
    assert not recover(c)
    assert not recover(c) and len(c.calls) == 1
    assert c.task.action_status == 'RUNNING' and c.store.get(c.plan).state == 'SendPressed'
    c.now += timedelta(seconds=61)
    c.gateway.verify_existing_receipt = lambda p: c.calls.append(p) or {'verified': True}
    assert recover(c) and len(c.calls) == 2


def test_identity_is_rechecked_after_desktop_observation(receipt_case):
    c = receipt_case
    def changed(p):
        c.order.forward_tracking_number = 'OTHER'
        c.db.commit()
        return {'verified': True}
    c.gateway.verify_existing_receipt = changed
    assert not recover(c)
    assert c.ledger.latest(77).state == 'SendPressed'
    assert c.store.get(c.plan).state == 'SendPressed'


def test_database_completed_before_ledger_crash_can_recover(receipt_case):
    c = receipt_case
    c.store.update(c.plan, 'Sent')
    c.task.action_status = 'SUCCEEDED'
    c.db.commit()
    assert recover(c)
    assert c.task.attempts == 1


def test_worker_rechecks_before_returning_blocked_and_resumes_same_cycle(receipt_case, monkeypatch):
    from contextlib import nullcontext

    from aftersales_workbench.workflows import module1_worker, windows_wecom

    c = receipt_case
    worker = object.__new__(module1_worker.Module1WorkerRuntime)
    worker.settings = NS(
        module1_desktop_send_enabled=True, module1_desktop_batch_limit=1,
        module1_desktop_lock_path=str(c.ledger.path.with_suffix('.lock')),
        module1_desktop_ledger_path=str(c.ledger.path), module1_desktop_group_map={'384': '测试快递群'},
        kuaidi100_carrier_map={}, module1_notification_min_task_id=0,
        module1_desktop_process_name='WXWork.exe')
    worker.options = NS(task_limit=1)
    monkeypatch.setattr(module1_worker, 'SessionLocal', lambda: nullcontext(c.db))
    monkeypatch.setattr(module1_worker.Module1WorkerRuntime, '_active_pdd_shop_codes',
                        property(lambda self: ('pdd-test',)))
    monkeypatch.setattr(windows_wecom, 'WindowsWeComGateway', lambda **kw: c.gateway)
    result = worker._process_desktop_notifications()
    assert result.status == 'completed' and result.error is None
    assert len(c.calls) == 1 and c.task.action_status == 'SUCCEEDED'
    assert result.details['sent'] == 0
