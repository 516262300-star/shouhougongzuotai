"""完整客户销售+实时平台合包核验；不访问外部写接口。"""

from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.integrations.erp.package_orders import CustomerSales
from aftersales_workbench.workflows.desktop_sender import (
    DesktopNoticeLedger,
    DesktopNoticeSendService,
)
from aftersales_workbench.workflows.notice_package_guard import KEY, NoticePackageGuard
from aftersales_workbench.workflows.tmall_notice_package import TmallNoticePackageVerifier
from tests import test_tmall_trade_intercept as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


def notice_guard(case):
    rows = tuple(dict(order_sn="8001", sale_sn=f"RC-{i}", sale_id=str(i), product="MODEL",
                      color="银色", quantity="2", sales_owner="测试业务员") for i in range(2))
    sales = CustomerSales("C1", "测试客户", "测试业务员", rows, 1)
    case.source = Mock(read=Mock(return_value=sales))
    verifier = TmallNoticePackageVerifier(case.db, case.cfg,
        source_factory=lambda: case.source, client_factory=lambda shop: nullcontext(case.client),
        platform=case.shop.platform)
    guard = NoticePackageGuard(case.db, case.cfg)
    setattr(guard, case.shop.platform.lower() + "_verifier", verifier)
    guard.trade_inspector = case.inspector
    return guard


@pytest.fixture(params=["TMALL", "TAOBAO"])
def case(db, request):
    x = base.case.__wrapped__(db)
    x.shop.platform = request.param
    db.delete(x.orders.pop())
    x.trade['orders']['order'] = x.trade['orders']['order'][:1]
    x.trade['payment'] = '20.00'
    x.orders[0].platform_order_amount = Decimal('20')
    x.orders[0].forward_tracking_number = 'JT-SYNTHETIC'
    x.orders[0].carrier_code = '极兔速递'
    x.orders[0].workflow_status = 'PENDING_CHECK'
    x.guard = notice_guard(x)
    rows = x.source.read.return_value.rows
    x.source.read.return_value = CustomerSales('C1', '测试客户', '测试业务员',
        rows + ({**rows[0], 'order_sn': '8002', 'sale_sn': 'RC-OTHER'},), 1)
    other = deepcopy(x.trade)
    other['tid'], other['status'] = '8002', 'WAIT_BUYER_CONFIRM_GOODS'
    other['orders']['order'] = [dict(oid='7003', num=2, outer_sku_id='MODEL#银色',
                                    refund_status='NO_REFUND')]
    x.trades = {'8001': x.trade, '8002': other}
    x.shipments = {'8001': x.logistics, '8002': deepcopy(x.logistics)}
    x.shipments['8002']['logistics_orders_get_response']['shippings']['shipping'][0]['tid'] = '8002'
    x.client.get_trade_fullinfo.side_effect = lambda tid: {
        'trade_fullinfo_get_response': {'trade': deepcopy(x.trades[str(tid)])}}
    x.client.get_logistics_orders.side_effect = lambda tid: deepcopy(x.shipments[str(tid)])
    x.task = Task(id=1, after_sales_sn='9001', action_type='QYWX_INTERCEPT_NOTIFY',
                  action_status='PENDING', attempts=0, idempotency_key='notice', payload={})
    db.add(x.task)
    db.commit()
    x.plan = base.plan_for(x, x.task)
    return x


def todos(db):
    return list(db.scalars(select(Task).where(Task.action_type == 'ERP_CREATE_MANUAL_TODO')))


def refund_sibling(case):
    case.trades['8002']['orders']['order'][0].update(refund_id='9003', refund_status='SUCCESS')
    case.refunds['9003'] = dict(tid='8002', oid='7003', refund_id='9003', refund_fee='20.00',
        status='SUCCESS', num=2, outer_id='MODEL#银色', has_good_return=False)


def test_single_tmall_refund_with_non_refunding_parcel_siblings_routes_manual(case, tmp_path):
    gateway = Mock()
    sender = DesktopNoticeSendService(case.db, gateway, DesktopNoticeLedger(tmp_path/'ledger'))
    sender.package_guard = case.guard
    result = sender.run([case.plan])
    assert result.sent == 0 and result.blocked_package == 1
    gateway.send.assert_not_called()
    assert case.task.action_status == 'CANCELLED' and case.task.attempts == 0
    assert case.orders[0].refund_financial_status == 'SUCCESS'
    assert case.task.payload[KEY]['blockers'][0]['order_sn'] == '8002'
    task = todos(case.db)[0]
    assert task.payload['assigned_order_sns'] == ['8001']
    assert task.payload['assignee'] == '测试业务员'
    assert '不自动拦截整包裹' in task.payload['content']
    assert '8002' in task.payload['content'] and '勿重复退款' in task.payload['content']
    assert '已暂停自动退款' not in task.payload['content']
    assert not (tmp_path/'ledger').exists()
    assert len(todos(case.db)) == 1
    assert not case.guard.check(case.plan)
    case.client.agree_refund.assert_not_called()


def test_all_parcel_orders_refunded_allows_existing_flow(case):
    refund_sibling(case)
    assert case.guard.check(case.plan)
    assert case.task.payload[KEY]['result'] == 'PASS'
    case.guard.validate_before_input(case.task.id)
    assert todos(case.db) == []


def test_alternative_sender_also_blocks_partial_parcel(case):
    from aftersales_workbench.db.models import AutomationActionType
    from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot

    task = ExternalTaskSnapshot(case.task.id, case.task.after_sales_sn,
        AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        {"tracking_number": case.plan.tracking_number, "carrier_code": case.plan.carrier_id},
        case.plan.platform_order_sn, case.shop.shop_code)
    executor = ExternalActionExecutor(case.db, case.cfg)
    executor.notice_package_guard = case.guard
    assert not executor._notice_package_ready(task)
    assert case.task.action_status == "CANCELLED" and case.task.attempts == 0
    assert todos(case.db)[0].payload["assignee"] == "测试业务员"


def test_same_customer_other_parcel_does_not_block(case):
    shipping = case.shipments['8002']['logistics_orders_get_response']['shippings']['shipping'][0]
    shipping['out_sid'] = 'OTHER'
    assert case.guard.check(case.plan)
    assert case.task.payload[KEY]['excluded_order_sns'] == ['8002']
    assert len(case.task.payload[KEY]['package_orders']) == 1


@pytest.mark.parametrize('fault', ['partial_money', 'partial_quantity', 'return', 'withdrawn'])
def test_other_parcel_order_not_fully_refunding_is_manual(case, fault):
    refund_sibling(case)
    child = case.trades['8002']['orders']['order'][0]
    refund = case.refunds['9003']
    if fault == 'partial_money':
        refund['refund_fee'] = '10.00'
    elif fault == 'partial_quantity':
        refund['num'] = 1
    elif fault == 'return':
        refund['has_good_return'] = True
    else:
        child['refund_status'] = refund['status'] = 'CLOSED'
    assert not case.guard.check(case.plan)
    assert case.task.action_status == 'CANCELLED'
    assert len(todos(case.db)) == 1


@pytest.mark.parametrize('fault', ['erp', 'seller', 'trade', 'shipping', 'page', 'multiple',
                                 'refund_missing', 'refund_identity', 'unknown', 'delivered'])
def test_uncertain_package_defers_without_guessing_or_todo(case, fault):
    refund_sibling(case)
    shipping = case.shipments['8002']['logistics_orders_get_response']
    if fault == 'erp':
        case.source.read.side_effect = ValueError('分页缺失')
    elif fault == 'seller':
        case.client.get_seller.return_value = {
            'user_seller_get_response': {'user': {'user_id': '102'}}}
    elif fault == 'trade':
        case.trades['8002']['tid'] = 'WRONG'
    elif fault == 'shipping':
        shipping['shippings']['shipping'][0]['tid'] = '8003'
    elif fault == 'page':
        shipping['total_results'] = 2
    elif fault == 'multiple':
        shipping['shippings']['shipping'].append({**shipping['shippings']['shipping'][0],
                                                  'out_sid': 'OTHER'})
    elif fault == 'refund_missing':
        case.refunds['9003'].pop('has_good_return')
    elif fault == 'refund_identity':
        case.refunds['9003']['tid'] = '8003'
    elif fault == 'unknown':
        case.trades['8002']['orders']['order'][0]['refund_status'] = 'NEW_UNKNOWN'
    elif fault == 'delivered':
        case.trades['8002']['status'] = 'TRADE_FINISHED'
    assert not case.guard.check(case.plan)
    assert case.task.action_status == 'PENDING' and case.task.attempts == 0
    assert case.task.payload[KEY]['result'] == 'UNAVAILABLE'
    assert case.task.payload[KEY]['retry_after']
    assert todos(case.db) == []
    case.source.read.reset_mock()
    assert not case.guard.check(case.plan)
    case.source.read.assert_not_called()


def test_tmall_package_approval_expires_before_input(case):
    refund_sibling(case)
    assert case.guard.check(case.plan)
    original = case.guard.now
    case.guard.now = lambda: original() + timedelta(seconds=81)
    with pytest.raises(ValueError, match='过期'):
        case.guard.validate_before_input(case.task.id)


def test_sent_record_is_preserved(case):
    case.task.action_status = 'SUCCEEDED'
    case.db.commit()
    assert not case.guard.check(case.plan)
    assert case.task.action_status == 'SUCCEEDED' and todos(case.db) == []


def test_full_trade_aggregate_cannot_bypass_other_parent_order_in_same_parcel(db, tmp_path):
    x = base.case.__wrapped__(db)
    tasks = base.prepare(x)
    guard = notice_guard(x)
    rows = x.source.read.return_value.rows
    x.source.read.return_value = CustomerSales('C1', '测试客户', '测试业务员',
        rows + ({**rows[0], 'order_sn': '8002'},), 1)
    other = deepcopy(x.trade)
    other.update(tid='8002', status='WAIT_BUYER_CONFIRM_GOODS')
    other['orders']['order'] = [dict(oid='7003', refund_status='NO_REFUND', num=2,
                                    outer_sku_id='MODEL#银色')]
    x.client.get_trade_fullinfo.side_effect = lambda tid: {'trade_fullinfo_get_response': {
        'trade': deepcopy(x.trade if str(tid) == '8001' else other)}}
    def shipments(tid):
        result = deepcopy(x.logistics)
        result['logistics_orders_get_response']['shippings']['shipping'][0]['tid'] = str(tid)
        return result
    x.client.get_logistics_orders.side_effect = shipments
    gateway = Mock()
    sender = DesktopNoticeSendService(db, gateway, DesktopNoticeLedger(tmp_path/'ledger'))
    sender.package_guard = guard
    assert sender.run([base.plan_for(x, t) for t in tasks]).sent == 0
    gateway.send.assert_not_called()
    assert all(t.action_status == 'CANCELLED' and t.attempts == 0 for t in tasks)
    assert len(todos(db)) == 1


def test_known_other_order_not_in_customer_sales_prevents_false_single_parcel(case):
    from aftersales_workbench.db.models import AfterSalesOrder
    other = AfterSalesOrder(shop_id=1, after_sales_sn='9004', platform_order_sn='8004',
        after_sales_type='ONLY_REFUND', refund_amount=20, platform_order_amount=20,
        workflow_status='PENDING_CHECK', order_shipping_status='IN_TRANSIT',
        forward_tracking_number=case.plan.tracking_number, carrier_code='极兔速递')
    case.db.add(other)
    case.db.commit()
    assert not case.guard.check(case.plan)
    assert case.task.action_status == 'PENDING'
    assert case.task.payload[KEY]['result'] == 'UNAVAILABLE'
    case.client.get_trade_fullinfo.assert_not_called()
