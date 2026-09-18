from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlan
from aftersales_workbench.workflows.desktop_sender import (
    DesktopNoticeLedger,
    DesktopNoticeSendService,
)
from aftersales_workbench.workflows.notice_package_guard import KEY, NoticePackageGuard
from aftersales_workbench.workflows.pdd_reconciliation import PddFailedRefundReconciler
from aftersales_workbench.workflows.shared_package import HOLD_REASON
from tests import test_shared_package as shared

base = shared.base
todos = shared.todos


@pytest.fixture
def db():
    yield from shared.db.__wrapped__()


@pytest.fixture
def setup(db):
    return shared.setup.__wrapped__(db)


@pytest.fixture
def case(db, setup):
    x = setup
    task = db.get(Task, 1)
    task.action_status = "PENDING"
    task.attempts = 0
    x.order.workflow_status = "PENDING_CHECK"
    db.commit()
    plan = DesktopNoticePlan(task_id=1, target_group="test", message="test",
                             after_sales_sn=x.order.after_sales_sn,
                             platform_order_sn=x.order.platform_order_sn,
                             tracking_number=x.order.forward_tracking_number,
                             carrier_id=x.order.carrier_code)
    guard = NoticePackageGuard(db, x.cfg, verifier=x.verifier,
                               client_factory=lambda shop: nullcontext(x.client),
                               now=lambda: base.NOW)
    return x, task, plan, guard


def test_partial_package_cancels_notice_before_ui_and_creates_one_todo(db, case, tmp_path):
    x, task, plan, guard = case
    gateway = Mock()
    service = DesktopNoticeSendService(db, gateway, DesktopNoticeLedger(tmp_path / 'ledger'))
    service.package_guard = guard
    result = service.run([plan, plan])
    assert result.sent == 0 and result.blocked_package == 2
    gateway.send.assert_not_called()
    assert task.action_status == 'CANCELLED' and task.attempts == 0
    assert x.order.exception_type == HOLD_REASON
    assert len(todos(db)) == 1
    assert '不自动拦截整包裹' in todos(db)[0].payload['content']
    assert not (tmp_path / 'ledger').exists()
    x.client.agree_refund.assert_not_called()


def test_all_same_parcel_orders_refund_allows_send_preflight(db, case):
    x, task, plan, guard = case
    x.infos['other']['refund_status'] = 2
    assert guard.check(plan)
    assert task.payload[KEY]['phase'] == 'before_notice'
    guard.validate_before_input(1)
    assert task.action_status == 'PENDING' and todos(db) == []


def test_approved_package_completes_existing_send_hooks(db, case, tmp_path):
    x, task, plan, guard = case
    x.infos['other']['refund_status'] = 2

    def send(plan, hooks):
        hooks.paste_started()
        hooks.send_pressed()
        hooks.sent()

    service = DesktopNoticeSendService(db, Mock(send=send),
                                       DesktopNoticeLedger(tmp_path / 'ledger'))
    service.package_guard = guard
    result = service.run([plan])
    assert result.sent == 1 and not result.error
    assert task.action_status == 'SUCCEEDED'
    x.client.agree_refund.assert_not_called()


def test_hold_cancels_all_unsent_siblings_and_survives_refund_changes(db, case):
    from aftersales_workbench.db.models import AfterSalesOrder

    x, task, plan, guard = case
    sibling = AfterSalesOrder(
        shop_id=x.order.shop_id, after_sales_sn='9002', platform_order_sn='other',
        after_sales_type='ONLY_REFUND', refund_amount=18.47, platform_order_amount=18.47,
        order_shipping_status='IN_TRANSIT', workflow_status='PENDING_CHECK',
        forward_tracking_number=x.order.forward_tracking_number, carrier_code=x.order.carrier_code,
    )
    pending = Task(after_sales_sn='9002', action_type='QYWX_INTERCEPT_NOTIFY',
                   action_status='PENDING', idempotency_key='sibling', attempts=0, payload={})
    db.add_all([sibling, pending])
    db.commit()
    assert not guard.check(plan)
    assert pending.action_status == 'CANCELLED' and pending.attempts == 0
    assert sibling.exception_type == HOLD_REASON and len(todos(db)) == 1
    # 新退款不能无人值守解除既有人工锁，也不能创建第二份同包裹待办。
    x.infos['other']['refund_status'] = 2
    task.action_status = 'PENDING'
    db.commit()
    x.source.read.reset_mock()
    assert not guard.check(plan)
    x.source.read.assert_not_called()
    assert len(todos(db)) == 1


def test_query_failure_defers_without_guessing_or_creating_todo(db, case):
    x, task, plan, guard = case
    x.source.read.side_effect = ValueError('ERP分页不完整')
    assert not guard.check(plan)
    assert task.action_status == 'PENDING' and todos(db) == []
    assert task.payload[KEY]['result'] == 'UNAVAILABLE'
    x.source.read.reset_mock()
    assert not guard.check(plan)
    x.source.read.assert_not_called()
    assert datetime_from(task.payload[KEY]['retry_after']) == base.NOW + timedelta(minutes=5)


def datetime_from(value):
    from datetime import datetime
    return datetime.fromisoformat(value)


@pytest.mark.parametrize('change', ['amount', 'tracking', 'expiry'])
def test_approved_snapshot_must_still_match_before_typing(db, case, change):
    x, task, plan, guard = case
    x.infos['other']['refund_status'] = 2
    assert guard.check(plan)
    if change == 'amount':
        x.order.refund_amount += 1
    elif change == 'tracking':
        x.order.forward_tracking_number = 'changed'
    else:
        guard.now = lambda: base.NOW + timedelta(seconds=81)
    db.commit()
    with pytest.raises(ValueError):
        guard.validate_before_input(1)


def test_mismatched_plan_never_checks_or_sends(db, case):
    x, task, plan, guard = case
    with pytest.raises(ValueError):
        guard.check(replace(plan, tracking_number='other'))
    x.source.read.assert_not_called()


def test_already_sent_notice_is_not_cancelled(db, case):
    x, task, plan, guard = case
    task.action_status = 'SUCCEEDED'
    db.commit()
    assert not guard.check(plan)
    assert task.action_status == 'SUCCEEDED' and todos(db) == []


def test_reconciliation_preserves_package_hold_and_no_generic_todo(db, case):
    x, task, plan, guard = case
    assert not guard.check(plan)
    x.task.action_status = 'FAILED'
    db.commit()
    PddFailedRefundReconciler(db, x.cfg).apply_observation(
        x.task, x.order, 2, x.order.refund_amount,
    )
    assert x.order.exception_type == HOLD_REASON and x.task.last_error == HOLD_REASON
    assert len(todos(db)) == 1
