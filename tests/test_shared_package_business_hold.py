from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import MoneyOperation
from aftersales_workbench.workflows import actions
from aftersales_workbench.workflows.module1_manual_todo import SqlAlchemyModule1ManualTodoRepository
from aftersales_workbench.workflows.pdd_reconciliation import PddFailedRefundReconciler
from aftersales_workbench.workflows.shared_package import (
    HOLD_REASON,
    PackageRefundHeld,
    mark_refund_business_hold,
    redundant_refund_failure_todo,
)
from tests import test_shared_package as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def held(db):
    x = base.setup.__wrapped__(db)
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, x.task.id)
    return x


def generic_payload():
    return dict(origin='module1', reason_code='MANUAL_PROCESSING',
                reason_text='退款失败或平台状态变化，需人工核验',
                assignee='示例业务员', marker='test', content='test', started_at='2026-09-18')


def test_executor_counts_business_hold_as_skipped_not_failed(db, held, monkeypatch):
    x = held
    x.task.action_status = 'PENDING'
    db.commit()
    monkeypatch.setattr(actions, 'load_configured_pdd_shops', lambda *a, **kw: [
        SimpleNamespace(shop_code='pdd-shop-01', credentials=lambda: object()),
    ])
    monkeypatch.setattr(actions, 'PddClient', lambda *a, **kw: x.client)
    executor = actions.ExternalActionExecutor(db, x.cfg, package_verifier=x.verifier)
    monkeypatch.setattr(executor, '_refresh_module1_refund_gates', lambda *a: None)
    monkeypatch.setattr(executor, '_agree_pdd', lambda *a:
                        x.verifier.require_before_refund(x.order, x.client, x.task.id))
    result = executor.run(action_types=('PDD_AGREE_REFUND',), dry_run=False)
    assert result.skipped == 1 and result.failed == result.succeeded == 0
    assert result.failed_task_ids == [] and x.task.action_status == 'CANCELLED'
    assert x.task.payload['execution_outcome'] == 'BUSINESS_HOLD'
    x.client.agree_refund.assert_not_called()


def test_generic_todo_generation_and_publish_are_both_suppressed(db, held, monkeypatch):
    x = held
    x.order.exception_type = generic_payload()['reason_text']
    db.commit()
    repo = SqlAlchemyModule1ManualTodoRepository(db)
    assert repo.list_candidates(shop_codes=None, limit=20) == []
    payload = generic_payload()
    assert redundant_refund_failure_todo(db, payload, x.order.after_sales_sn)
    assert not redundant_refund_failure_todo(db, base.todos(db)[0].payload, x.order.after_sales_sn)
    base.todos(db)[0].action_status = 'SUCCEEDED'
    queued = Task(after_sales_sn=x.order.after_sales_sn, action_type='ERP_CREATE_MANUAL_TODO',
                  action_status='PENDING', idempotency_key='old-generic',
                  attempts=0, payload=payload)
    db.add(queued)
    db.commit()
    x.cfg.erp_write_enabled = x.cfg.erp_todo_publish_enabled = True
    client = Mock()
    executor = actions.ExternalActionExecutor(db, x.cfg)
    monkeypatch.setattr(executor, '_build_erp_todo_client', lambda: client)
    result = executor.run(action_types=('ERP_CREATE_MANUAL_TODO',), dry_run=False)
    assert result.skipped == 1 and queued.action_status == 'CANCELLED' and queued.attempts == 0
    client.create_todo.assert_not_called()


def test_readonly_reconciliation_reclassifies_old_false_failure(db, held):
    x = held
    x.task.action_status = 'FAILED'
    x.task.last_error = x.order.exception_type = generic_payload()['reason_text']
    db.commit()
    PddFailedRefundReconciler(db, x.cfg).apply_observation(
        x.task, x.order, 2, x.order.refund_amount,
    )
    assert x.task.action_status == 'CANCELLED' and x.task.last_error == HOLD_REASON
    assert x.order.exception_type == HOLD_REASON
    assert len(base.todos(db)) == 1


def test_business_hold_still_gets_readonly_platform_success_check(db, held, monkeypatch):
    x = held
    assert mark_refund_business_hold(db, x.task, x.order)
    db.commit()
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.get_refund_information.return_value = dict(
        id=int(x.order.after_sales_sn), order_sn=x.order.platform_order_sn,
        after_sales_status=10, refund_amount=1847,
    )
    import aftersales_workbench.workflows.pdd_reconciliation as module
    monkeypatch.setattr(module, 'load_configured_pdd_shops', lambda *a, **kw: [
        SimpleNamespace(shop_code='pdd-shop-01'),
    ])
    reconciler = PddFailedRefundReconciler(db, x.cfg, client_factory=lambda shop: client)
    result = reconciler.run(dry_run=False)
    assert result['confirmed_success'] == 1 and x.task.action_status == 'SUCCEEDED'
    client.agree_refund.assert_not_called()


@pytest.mark.parametrize('evidence', ['request_marker', 'UNKNOWN', 'CONFIRMED'])
def test_real_funds_evidence_is_never_downgraded_or_hidden(db, held, evidence):
    x = held
    x.task.action_status = 'FAILED'
    if evidence == 'request_marker':
        x.task.payload = {
            **x.task.payload, 'uncollected_request_started_at': '2026-09-18T01:00:00Z',
        }
    else:
        db.add(MoneyOperation(operation_key='actual-request', platform='PDD',
                              shop_id=x.order.shop_id, after_sales_sn=x.order.after_sales_sn,
                              operation_type='PLATFORM_REFUND', task_id=x.task.id, state=evidence,
                              started_at=base.base.NOW, updated_at=base.base.NOW, snapshot={}))
    x.order.exception_type = generic_payload()['reason_text']
    db.commit()
    assert not mark_refund_business_hold(db, x.task, x.order)
    assert not redundant_refund_failure_todo(db, generic_payload(), x.order.after_sales_sn)
    repo = SqlAlchemyModule1ManualTodoRepository(db)
    assert len(repo.list_candidates(shop_codes=None, limit=20)) == 1
    PddFailedRefundReconciler(db, x.cfg).apply_observation(
        x.task, x.order, 2, x.order.refund_amount,
    )
    assert x.task.action_status == 'FAILED'
