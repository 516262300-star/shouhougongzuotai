"""合成多子单退款；平台、ERP和企业微信均使用替身。"""

from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder as Order
from aftersales_workbench.db.models import Shop
from aftersales_workbench.integrations.tmall.mapper import normalize_refund
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.services.refund_scope import PARTIAL_REFUND_NOTE, reconcile_refund_scope
from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlan
from aftersales_workbench.workflows.desktop_sender import (
    DesktopNoticeLedger,
    DesktopNoticeSendService,
)
from aftersales_workbench.workflows.module1 import (
    Module1InterceptService,
    SqlAlchemyModule1Repository,
)
from aftersales_workbench.workflows.module1_preflight import Module1NotificationPreflightService
from aftersales_workbench.workflows.tmall_trade_intercept import (
    KEY,
    TradeInspector,
    collect_candidates,
    inspect_trade,
)
from tests import test_pdd_non_refund_sync as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def case(db):
    shop = Shop(shop_id=1, platform="TMALL", shop_code="tmall-shop-01", shop_name="测试店",
                platform_shop_id="101", is_active=1)
    trade = dict(tid="8001", status="TRADE_CLOSED", payment="50.00",
                 consign_time="2026-09-20 08:00:00", orders={"order": []})
    refunds = {}
    orders = []
    for i, amount in enumerate(("20.00", "30.00"), 1):
        oid, rid, sku = str(7000 + i), str(9000 + i), f"MODEL-{i}#银色"
        trade['orders']['order'].append(dict(oid=oid, refund_id=rid, refund_status="SUCCESS",
                                            num=2, outer_sku_id=sku, payment="0.00"))
        refunds[rid] = dict(tid="8001", oid=oid, refund_id=rid, refund_fee=amount,
                            payment="0.00", status="SUCCESS", num=2, outer_id=sku,
                            has_good_return=False, order_status="TRADE_CLOSED")
        orders.append(Order(id=i, shop_id=1, platform_order_sn="8001", after_sales_sn=rid,
            after_sales_type="ONLY_REFUND", refund_amount=Decimal(amount),
            platform_order_amount=Decimal("50.00"), refund_financial_status="SUCCESS",
            platform_after_sales_status_text="SUCCESS", order_shipping_status="UNKNOWN",
            workflow_status="PARTIAL_REFUND_EXCLUDED", exception_type=PARTIAL_REFUND_NOTE))
    logistics = {"logistics_orders_get_response": {"shippings": {"shipping": [dict(
        tid="8001", out_sid="JT-SYNTHETIC", company_name="极兔速递", seller_confirm="yes")]}}}
    steps = [dict(status_time="2026-09-20 09:00:00", status_desc="快件运输中", action="ACCEPT")]
    client = Mock()
    client.get_seller.return_value = {"user_seller_get_response": {"user": {"user_id": "101"}}}
    client.get_trade_fullinfo.side_effect = lambda **kw: {"trade_fullinfo_get_response": {
        "trade": deepcopy(trade)}}
    client.get_refund.side_effect = lambda refund_id: {"refund_get_response": {
        "refund": deepcopy(refunds[str(refund_id)])}}
    client.get_logistics_orders.side_effect = lambda **kw: deepcopy(logistics)
    client.execute_read.side_effect = lambda *a, **kw: {"logistics_trace_search_response": {
        "tid": "8001", "out_sid": "JT-SYNTHETIC", "company_name": "极兔速递",
        "status": "对方已签收", "trace_list": {"transit_step_info": deepcopy(steps)}}}
    db.add_all([shop, *orders])
    db.commit()
    cfg = Settings(_env_file=None)
    inspector = TradeInspector(db, cfg, client_factory=lambda shop: nullcontext(client))
    return SimpleNamespace(db=db, shop=shop, orders=orders, trade=trade, refunds=refunds,
        logistics=logistics, client=client, inspector=inspector, cfg=cfg, steps=steps)


def prepare(case):
    candidates, errors = collect_candidates(case.db, shop_codes=(case.shop.shop_code,),
        min_order_id=0, limit=20, inspector=case.inspector)
    assert not errors
    repo = SqlAlchemyModule1Repository(case.db)
    for c in candidates:
        repo.enqueue_notice(c)
    case.db.commit()
    return list(case.db.scalars(select(Task).order_by(Task.id)))


def test_closed_trade_zero_child_payment_combines_without_changing_amounts(case):
    tasks = prepare(case)
    assert len(tasks) == 2
    assert [o.refund_amount for o in case.orders] == [Decimal('20'), Decimal('30')]
    assert all(o.platform_order_amount == Decimal('50') for o in case.orders)
    assert all(o.workflow_status == 'PENDING_CHECK' and o.forward_tracking_number == 'JT-SYNTHETIC'
               for o in case.orders)
    assert all(t.payload[KEY]['buyer_paid'] == '50.00' for t in tasks)
    assert all(t.action_type == 'QYWX_INTERCEPT_NOTIFY' for t in tasks)
    case.client.agree_refund.assert_not_called()


@pytest.mark.parametrize('fault', ['partial', 'missing_refund', 'refused', 'withdrawn',
    'duplicate_oid', 'duplicate_refund', 'quantity', 'sku', 'return_refund', 'cashback',
    'wrong_order', 'wrong_oid', 'status_conflict', 'invalid_amount', 'second_parcel',
    'wrong_shipping', 'unshipped', 'delivered', 'api_error'])
def test_incomplete_or_conflicting_trade_never_becomes_candidate(case, fault):
    c = case.trade['orders']['order'][0]
    r = case.refunds['9001']
    if fault == 'partial':
        r['refund_fee'] = '19.00'
    elif fault == 'missing_refund':
        c.pop('refund_id')
    elif fault in ('refused', 'withdrawn'):
        c['refund_status'] = 'SELLER_REFUSE_BUYER' if fault == 'refused' else 'CLOSED'
    elif fault == 'duplicate_oid':
        case.trade['orders']['order'][1]['oid'] = c['oid']
    elif fault == 'duplicate_refund':
        case.trade['orders']['order'][1]['refund_id'] = c['refund_id']
    elif fault == 'quantity':
        r['num'] = 1
    elif fault == 'sku':
        r['outer_id'] = 'OTHER'
    elif fault == 'return_refund':
        r['has_good_return'] = True
    elif fault == 'cashback':
        r['special_refund_type'] = 'cashBack'
    elif fault == 'wrong_order':
        r['tid'] = '8002'
    elif fault == 'wrong_oid':
        r['oid'] = '7003'
    elif fault == 'status_conflict':
        r['status'] = 'WAIT_SELLER_AGREE'
    elif fault == 'invalid_amount':
        r['refund_fee'] = 'NaN'
    elif fault == 'second_parcel':
        case.logistics['logistics_orders_get_response']['shippings']['shipping'].append(dict(
            tid='8001', out_sid='OTHER', company_name='极兔速递', seller_confirm='yes'))
    elif fault == 'wrong_shipping':
        case.logistics['logistics_orders_get_response']['shippings']['shipping'][0]['tid'] = '8002'
    elif fault == 'unshipped':
        case.trade.pop('consign_time')
        case.trade['status'] = 'WAIT_SELLER_SEND_GOODS'
        row = case.logistics['logistics_orders_get_response']['shippings']['shipping'][0]
        row['seller_confirm'] = 'no'
    elif fault == 'delivered':
        case.trade['status'] = 'TRADE_FINISHED'
    elif fault == 'api_error':
        case.client.get_refund.side_effect = TimeoutError('synthetic')
    candidates, errors = collect_candidates(case.db, shop_codes=None, min_order_id=0,
                                             limit=20, inspector=case.inspector)
    assert candidates == [] and len(errors) == 1
    assert case.db.scalar(select(Task.id)) is None


def test_partly_pending_and_successful_refunds_cover_whole_trade(case):
    case.trade['orders']['order'][0]['refund_status'] = 'WAIT_SELLER_AGREE'
    case.refunds['9001']['status'] = 'WAIT_SELLER_AGREE'
    case.orders[0].platform_after_sales_status_text = 'WAIT_SELLER_AGREE'
    case.orders[0].refund_financial_status = 'UNKNOWN'
    case.db.commit()
    assert len(prepare(case)) == 2


def test_mapper_keeps_parent_paid_amount_when_child_has_balance(case):
    case.refunds['9001']['payment'] = '20.00'
    result = normalize_refund(case.refunds['9001'], case.refunds['9001'], case.trade)
    assert result.platform_order_amount == Decimal('50.00')


def test_repeated_scan_and_sync_preserve_notice_but_not_money_permission(case):
    tasks = prepare(case)
    for o in case.orders:
        reconcile_refund_scope(case.db, o)
    case.db.commit()
    assert all(t.action_status == 'PENDING' for t in tasks)
    assert all(o.workflow_status == 'PENDING_CHECK' for o in case.orders)
    assert len(prepare(case)) == 2
    assert case.db.scalar(select(Task).where(Task.action_type != 'QYWX_INTERCEPT_NOTIFY')) is None


def test_own_shop_watermark_manual_lock_and_local_conflict(case):
    for kwargs in ({'shop_codes':('other',), 'min_order_id':0},
                   {'shop_codes':None, 'min_order_id':3}):
        assert collect_candidates(case.db, **kwargs, limit=20, inspector=case.inspector)[0] == []
    case.orders[0].workflow_status = 'MANUAL_PROCESSING'
    case.db.commit()
    assert collect_candidates(case.db, shop_codes=None, min_order_id=0, limit=20,
                              inspector=case.inspector)[0] == []


def preflight(case):
    query = Mock()
    service = Module1NotificationPreflightService(case.db, query)
    service.trade_inspector = case.inspector
    return service, query


def test_returning_native_node_cancels_notices_and_tracks_return(case):
    tasks = prepare(case)
    case.steps[0]['status_desc'] = '包裹退回中'
    service, query = preflight(case)
    result = service.run(dry_run=False)
    assert result.returning_skipped == 2
    assert all(t.action_status == 'CANCELLED' for t in tasks)
    assert all(o.workflow_status == 'INTERCEPT_REFUNDED_WAITING_RETURN' for o in case.orders)
    query.query.assert_not_called()
    assert len(list(case.db.scalars(select(Task)))) == 2


def test_top_level_signed_placeholder_never_cancels_transit_notice(case):
    tasks = prepare(case)
    service, query = preflight(case)
    result = service.run(dry_run=False)
    assert result.in_transit_ready == 2
    assert all(t.action_status == 'PENDING' for t in tasks)
    query.query.assert_not_called()


def test_scope_visible_in_list_and_intercepts(case):
    prepare(case)
    service = AftersalesRecordService(case.db)
    items = service.list_intercepts(page=1, page_size=15, keyword='8001')
    assert items['pagination']['total'] == 2
    tasks = list(case.db.scalars(select(Task)))
    assert service._refund_scope(case.orders[0], tasks) == '多笔合计全额退款'


def plan_for(case, task):
    return DesktopNoticePlan(task_id=task.id, target_group='测试极兔群', message='合成拦截消息',
        after_sales_sn=task.after_sales_sn, platform_order_sn='8001',
        tracking_number='JT-SYNTHETIC', carrier_id='极兔速递')


def test_send_rechecks_all_refunds_and_same_parcel_sends_once(case, tmp_path):
    tasks = prepare(case)
    preflight(case)[0].run(dry_run=False)
    from tests.test_tmall_notice_package import notice_guard
    guard = notice_guard(case)
    def send(plan, hooks):
        hooks.paste_started()
        hooks.send_pressed()
        hooks.sent()
    gateway = Mock(send=send)
    sender = DesktopNoticeSendService(case.db, gateway, DesktopNoticeLedger(tmp_path/'ledger'))
    sender.package_guard = guard
    result = sender.run([plan_for(case, t) for t in tasks])
    assert result.sent == 1
    assert all(t.action_status == 'SUCCEEDED' for t in tasks)
    assert all(o.workflow_status == 'INTERCEPT_PUSHED' for o in case.orders)
    case.client.agree_refund.assert_not_called()


def test_withdrawal_before_send_does_not_touch_wecom(case, tmp_path):
    tasks = prepare(case)
    preflight(case)[0].run(dry_run=False)
    case.refunds['9002']['status'] = 'CLOSED'
    from tests.test_tmall_notice_package import notice_guard
    guard = notice_guard(case)
    gateway = Mock()
    sender = DesktopNoticeSendService(case.db, gateway, DesktopNoticeLedger(tmp_path/'ledger'))
    sender.package_guard = guard
    result = sender.run([plan_for(case, tasks[0])])
    assert result.sent == 0 and result.blocked_package == 1
    gateway.send.assert_not_called()


def test_expiry_or_change_before_typing_is_blocked(case):
    tasks = prepare(case)
    from tests.test_tmall_notice_package import notice_guard
    guard = notice_guard(case)
    assert guard.check(plan_for(case, tasks[0]))
    guard.now = lambda: datetime.now(UTC) + timedelta(seconds=81)
    with pytest.raises(ValueError):
        guard.validate_before_input(tasks[0].id)


def test_service_dry_run_reads_but_does_not_repair_or_enqueue(case, monkeypatch):
    monkeypatch.setattr(TradeInspector, '_client', lambda self, shop: nullcontext(case.client))
    result = Module1InterceptService(SqlAlchemyModule1Repository(case.db)).run(
        include_tmall=True, dry_run=True)
    assert result.scanned == 2 and result.tasks_created == 0
    assert case.db.scalar(select(Task.id)) is None
    assert all(o.forward_tracking_number is None for o in case.orders)


def test_raw_trade_verifier_returns_minimal_non_personal_proof(case):
    proof = inspect_trade(case.client, '8001')
    assert proof['buyer_paid'] == '50.00' and len(proof['refunds']) == 2
    assert 'buyer_nick' not in str(proof) and 'phone' not in str(proof)


def test_pending_multi_refund_does_not_acquire_money_execution(case):
    for child in case.trade['orders']['order']:
        child['refund_status'] = 'WAIT_SELLER_AGREE'
    for refund in case.refunds.values():
        refund['status'] = 'WAIT_SELLER_AGREE'
    for order in case.orders:
        order.platform_after_sales_status_text = 'WAIT_SELLER_AGREE'
        order.refund_financial_status = 'UNKNOWN'
    case.db.commit()
    prepare(case)
    case.steps[0]['status_desc'] = '包裹退回中'
    service, query = preflight(case)
    service.tmall_refund_shop_codes = {'tmall-shop-01'}
    service.run(dry_run=False)
    assert case.db.scalar(select(Task).where(Task.action_type == 'TMALL_AGREE_REFUND')) is None
    assert all(o.exception_type == '多子单已合并拦截，退款须逐笔核验' for o in case.orders)


def test_return_followup_uses_native_trace_when_third_party_is_unavailable(case):
    from aftersales_workbench.workflows.module1_logistics import Module1LogisticsGateService

    prepare(case)
    case.steps[0]['status_desc'] = '包裹退回中'
    preflight(case)[0].run(dry_run=False)
    case.steps.append(dict(status_time='2026-09-21 08:00:00', status_desc='退回件已签收'))
    query = Mock()
    service = Module1LogisticsGateService(case.db, query)
    service.trade_inspector = case.inspector
    result = service.run(dry_run=False, force_refresh=True)
    assert result.failed == 0
    assert all(o.workflow_status == 'RETURN_WAITING_ERP_MATCH' for o in case.orders)
    assert len(list(case.db.scalars(select(Task).where(
        Task.action_type == 'ERP_MATCH_RETURN_ORDER')))) == 2
    query.query.assert_not_called()


def test_wrong_seller_cannot_supply_trade_evidence(case):
    case.client.get_seller.return_value = {'user_seller_get_response': {'user': {'user_id': '102'}}}
    candidates, errors = collect_candidates(case.db, shop_codes=None, min_order_id=0,
        limit=20, inspector=case.inspector)
    assert candidates == [] and '授权身份' in errors[0]['reason']


def test_failed_history_checks_rotate_instead_of_blocking_later_orders(case):
    from aftersales_workbench.db.models import AutomationPollState

    for n in range(2, 14):
        for i, amount in enumerate((20, 30)):
            case.db.add(Order(shop_id=1, platform_order_sn=str(8000+n),
                after_sales_sn=str(10000+n*2+i), after_sales_type='ONLY_REFUND',
                refund_amount=amount, platform_order_amount=50,
                platform_after_sales_status_text='SUCCESS',
                workflow_status='PARTIAL_REFUND_EXCLUDED',
                order_shipping_status='UNKNOWN'))
    case.db.commit()
    inspector = Mock()
    inspector.inspect.side_effect = ValueError('synthetic verification unavailable')
    first = collect_candidates(case.db, shop_codes=None, min_order_id=0, limit=50,
                               inspector=inspector, dry_run=False)[1]
    case.db.commit()
    second = collect_candidates(case.db, shop_codes=None, min_order_id=0, limit=50,
                                inspector=inspector, dry_run=False)[1]
    case.db.commit()
    assert len(first) == 10 and len(second) == 3
    assert {e['order'] for e in first}.isdisjoint({e['order'] for e in second})
    assert len(list(case.db.scalars(select(AutomationPollState)))) == 13


def test_known_unsent_partial_cancellation_reuses_original_task_identity(case):
    old = Task(after_sales_sn='9001', action_type='QYWX_INTERCEPT_NOTIFY',
        action_status='CANCELLED', last_error=PARTIAL_REFUND_NOTE, attempts=0,
        idempotency_key='workflow:9001:QYWX_INTERCEPT_NOTIFY', payload={})
    case.db.add(old)
    case.db.commit()
    old_id = old.id
    tasks = prepare(case)
    assert len(tasks) == 2 and tasks[0].id == old_id and tasks[0].action_status == 'PENDING'


def test_unknown_or_attempted_notification_is_not_recreated(case):
    old = Task(after_sales_sn='9001', action_type='QYWX_INTERCEPT_NOTIFY',
        action_status='RUNNING', attempts=1, idempotency_key='uncertain', payload={})
    case.db.add(old)
    case.db.commit()
    tasks = prepare(case)
    assert len(tasks) == 2 and tasks[0].action_status == 'RUNNING'
    # 第二条仍受既有桌面包裹永久账本及RUNNING检查，不能藉此重发。
