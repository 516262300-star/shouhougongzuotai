"""Web独立发布版本的只读展示，不依赖Worker发送器实现。"""

from decimal import Decimal

import pytest

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder as Order
from aftersales_workbench.db.models import Shop
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.workflows.tmall_trade_intercept import KEY
from tests import test_pdd_non_refund_sync as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


def test_full_trade_records_show_original_amounts_and_return_tracking(db):
    db.add(Shop(shop_id=1, platform='TMALL', shop_name='测试店', shop_code='tmall-shop-01',
                platform_shop_id='101', is_active=1))
    proof = dict(result='FULL_TRADE_ONLY_REFUND', shop_id=1, order_sn='8001',
        buyer_paid='50.00', tracking_number='JT-SYNTHETIC', carrier='极兔速递',
        refunds=[dict(refund_id='9001', oid='7001', amount='20.00'),
                 dict(refund_id='9002', oid='7002', amount='30.00')])
    for i, amount in enumerate(('20.00', '30.00'), 1):
        rid = str(9000+i)
        db.add(Order(id=i, shop_id=1, platform_order_sn='8001', after_sales_sn=rid,
            after_sales_type='ONLY_REFUND', refund_amount=Decimal(amount),
            platform_order_amount=Decimal('50.00'), order_shipping_status='IN_TRANSIT',
            forward_tracking_number='JT-SYNTHETIC', carrier_code='极兔速递',
            platform_after_sales_status_text='SUCCESS', refund_financial_status='SUCCESS',
            workflow_status='INTERCEPT_REFUNDED_WAITING_RETURN', logistics_state='RETURNING'))
        db.add(Task(after_sales_sn=rid, action_type='QYWX_INTERCEPT_NOTIFY',
            action_status='CANCELLED', idempotency_key='test:'+rid, attempts=0,
            payload={KEY:proof}, last_error='包裹已退回中，不重复拦截'))
    db.commit()
    service = AftersalesRecordService(db)
    result = service.list_intercepts(page=1, page_size=15, keyword='8001')
    assert result['pagination']['total'] == 2
    listed = service.list_orders(page=1, page_size=15, keyword='8001', record_view='ALL')
    assert {r['refund_scope'] for r in listed['items']} == {'多笔合计全额退款'}
    assert {r['refund_amount'] for r in listed['items']} == {20.0, 30.0}
    assert {r['platform_order_amount'] for r in listed['items']} == {50.0}
    assert {r['logistics_state'] for r in listed['items']} == {'RETURNING'}
    assert service.get_order('9001')['refund_scope'] == '多笔合计全额退款'
