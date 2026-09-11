from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    Platform,
    ShippingStatus,
)
from aftersales_workbench.integrations.marketplace.alibaba_1688 import (
    Alibaba1688ReadClient,
    normalize_1688_refund,
)
from aftersales_workbench.integrations.marketplace.models import ConfiguredMarketplaceShop
from aftersales_workbench.integrations.marketplace.repository import (
    SqlAlchemyMarketplaceSyncRepository,
)
from aftersales_workbench.services.record_status import confirmed_refund, refund_display
from aftersales_workbench.workflows.module1 import SqlAlchemyModule1Repository
from aftersales_workbench.workflows.module3 import SqlAlchemyModule3Repository
from aftersales_workbench.workflows.money_operations import MoneyOperationBlocked, run_money_write
from tests import test_pdd_non_refund_sync as base


def detail(kind='return'):
    value = dict(refundId='SYNTH-1688', orderId='ORDER-1688', applyPayment=0,
                 applyCarriage=0, refundPayment=0, refundCarriage=0, onlyRefund=False,
                 orderEntryCountMap={'LINE-1':2}, status='refundsuccess')
    if kind == 'return':
        value.update(disputeRequest=3, refundGoods=True, newRefundReturn=True,
            extInfo={'refundFlowType':'zero_money','workflowName':'cbu_return_and_refund',
                     'b_replace_flag':'1'})
    else:
        value.update(disputeRequest=10, refundGoods=False, newRefundReturn=False,
            extInfo={'workflowName':'cbu_reshipping','serviceType':'_damaged_reshipping'})
    return value


def order_detail():
    return {'baseInfo':{'idOfStr':'ORDER-1688','status':'waitbuyerreceive','totalAmount':'20'},
            'productItems':[{'subItemID':'LINE-1','cargoNumber':'SKU#silver','itemAmount':'20'}]}


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


def repo_shop(db):
    config = ConfiguredMarketplaceShop(platform=Platform.ALIBABA_1688,shop_number=1,
        shop_code='1688-main',shop_name='synthetic',platform_shop_id='synthetic',
        app_key=SecretStr('key'),app_secret=SecretStr('secret'))
    repo = SqlAlchemyMarketplaceSyncRepository(db)
    sid = repo.upsert_shop(config)
    repo.commit()
    return repo, config, sid


@pytest.mark.parametrize('kind,expected', [('return','RETURN_AND_REFUND'),('resend','RESEND')])
@pytest.mark.parametrize('status', ['refundsuccess','refundclose','waitsellerreceive'])
def test_zero_flows_record_without_funding_or_automatic_tasks(db, kind, expected, status):
    raw = detail(kind)
    raw['status'] = status
    normalized = normalize_1688_refund(raw,order_detail())
    assert normalized.after_sales_type == expected and normalized.refund_amount == 0
    repo, config, sid = repo_shop(db)
    assert repo.upsert_refund(config,sid,normalized)
    repo.commit()
    assert not repo.upsert_refund(config,sid,normalized)
    repo.commit()
    order = db.scalar(select(AfterSalesOrder))
    assert order.refund_financial_status == 'NOT_APPLICABLE'
    assert order.actual_refund_amount is None and order.refund_completed_at is None
    assert not confirmed_refund(order,Platform.ALIBABA_1688)
    assert refund_display(order,Platform.ALIBABA_1688)['label'] == '不涉及退款'
    assert order.workflow_status == 'PENDING_CHECK'
    assert db.scalar(select(func.count()).select_from(AfterSalesOrder)) == 1
    assert SqlAlchemyModule1Repository(db).list_candidates(shop_codes=None,limit=20) == []
    order.order_shipping_status = ShippingStatus.UNSHIPPED
    db.flush()
    assert SqlAlchemyModule3Repository(db).list_candidates(
        shop_codes=None,platform_order_sn=None,limit=20) == []
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0
    def prohibited():
        pytest.fail('1688不能触发资金回调')
    with pytest.raises(MoneyOperationBlocked):
        run_money_write(db,order,operation_type='PLATFORM_REFUND',task_id=1,write=prohibited)


@pytest.mark.parametrize('field', ['applyPayment','applyCarriage','refundPayment','refundCarriage'])
@pytest.mark.parametrize('value', [None,'',1,'0.1','NaN',True])
def test_missing_or_conflicting_zero_money_field_stays_quarantined(field,value):
    raw = detail()
    raw[field] = value
    with pytest.raises(ValueError,match='金额'):
        normalize_1688_refund(raw,order_detail())


@pytest.mark.parametrize('field', ['refundFlowType','workflowName','b_replace_flag'])
def test_zero_amount_without_complete_flow_markers_is_not_accepted(field):
    raw = detail()
    del raw['extInfo'][field]
    with pytest.raises(ValueError,match='退款金额'):
        normalize_1688_refund(raw,order_detail())


@pytest.mark.parametrize('change', ['order_id','missing_items','duplicate_items','missing_counts',
                                   'unknown_item','fractional_quantity'])
def test_non_financial_import_requires_real_order_and_item_identity(change):
    raw, order = detail(), order_detail()
    if change == 'order_id':
        order['baseInfo']['idOfStr'] = 'another-order'
    elif change == 'missing_items':
        del order['productItems']
    elif change == 'duplicate_items':
        order['productItems'] *= 2
    elif change == 'missing_counts':
        del raw['orderEntryCountMap']
    elif change == 'unknown_item':
        raw['orderEntryCountMap'] = {'OTHER':2}
    else:
        raw['orderEntryCountMap'] = {'LINE-1':1.5}
    with pytest.raises(ValueError):
        normalize_1688_refund(raw,order)


def test_existing_refunded_fact_cannot_be_replaced_by_zero_flow(db):
    repo, config, sid = repo_shop(db)
    normalized = normalize_1688_refund(detail(),order_detail())
    repo.upsert_refund(config,sid,replace(normalized,refund_amount=Decimal('10')))
    repo.commit()
    order = db.scalar(select(AfterSalesOrder))
    assert order.refund_financial_status == 'SUCCESS'
    with pytest.raises(ValueError,match='已有资金事实'):
        repo.upsert_refund(config,sid,normalized)
    db.rollback()
    assert order.refund_amount == 10 and order.actual_refund_amount == 10


def test_order_query_timeout_does_not_become_zero_money_record():
    client = object.__new__(Alibaba1688ReadClient)
    client.get_refund_detail = lambda _: {'result':{'opOrderRefundModelDetail':detail()}}
    def unavailable(_):
        raise TimeoutError('synthetic read timeout')
    client.get_order_detail = unavailable
    with pytest.raises(TimeoutError):
        client.fetch_refund('SYNTH-1688')
