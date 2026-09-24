from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.workflows.alibaba_1688_shipment_source import (
    ORDER_LIST_API,
    Alibaba1688ShipmentClient,
    Alibaba1688ShipmentSource,
)
from tests import test_shipment_watch as base

SN = "800000000001"
SELLER = "2088000000000001"
FINGERPRINT = sha256(SELLER.encode()).hexdigest()


def trade():
    return {
        "baseInfo": {"id": int(SN), "idOfStr": SN, "sellerAlipayId": SELLER,
                     "sellerLoginId": "脱*", "sellerOrder": False,
                     "status": "waitbuyerreceive", "totalAmount": 20,
                     "refund": 0, "refundPayment": 0},
        "productItems": [{"subItemID": 801, "subItemIDString": "801",
                          "status": "waitbuyerreceive", "logisticsStatus": 2}],
        "nativeLogistics": {"logisticsItems": [{
            "type": "0", "status": "alreadysend", "logisticsBillNo": "SYNTH168801",
            "logisticsCompanyName": "极兔速递", "logisticsCompanyNo": "HTKY",
            "logisticsCompanyId": 352, "subItemIds": "801",
            "deliveredTime": "20260923120000000+0800",
        }]},
    }


@pytest.fixture
def setup():
    state = SimpleNamespace(row=trade(), calls=[], bodies=None)

    def execute(namespace, api, **params):
        state.calls.append((namespace, api, params))
        assert api == ORDER_LIST_API
        return state.bodies[params['page'] - 1] if state.bodies else {
            "success": True, "totalRecord": 1, "result": [state.row],
        }

    client = SimpleNamespace(execute_read=execute, get_order_detail=lambda sn: {
        "success": "true", "retCodes": ["BUYER_ENCRYPT"], "result": state.row,
    })
    return Alibaba1688ShipmentSource(client, seller_fingerprint=FINGERPRINT), state


def test_true_package_time_and_carrier_name_not_conflicting_platform_code(setup):
    source, state = setup
    row = state.row
    row['baseInfo']['allDeliveredTime'] = "20260924150000000+0800"
    parcel = source.refresh(SN)[0]
    assert parcel.shipped_at == datetime(2026, 9, 23, 4)
    assert parcel.carrier == "极兔速递"  # HTKY cannot be interpreted as legacy 百世.
    assert parcel.sub_order_ids == ("801",)
    assert source.candidate(row) == (SN, parcel.shipped_at)


def test_multiple_parcels_and_partial_refund(setup):
    source, state = setup
    state.row['baseInfo'].update(refund=5, refundPayment=500)
    p = state.row['nativeLogistics']['logisticsItems'][0]
    state.row['nativeLogistics']['logisticsItems'].append({
        **p, 'logisticsBillNo': 'SYNTH168802', 'deliveredTime': '20260924060000000+0800',
    })
    a, b = source.refresh(SN)
    assert b.shipped_at - a.shipped_at == timedelta(hours=18)
    assert source.candidate(state.row)[1] == a.shipped_at


def test_platform_full_yto_name_is_a_carrier_alias_not_a_number(setup):
    source, state = setup
    state.row['nativeLogistics']['logisticsItems'][0].update(
        logisticsCompanyName='圆通速递(YTO)', logisticsCompanyNo='YTO')
    assert source.refresh(SN)[0].carrier == '圆通速递'


def test_historical_ended_merchant_cannot_enter_queue_or_bypass_live_refresh(setup):
    source, state = setup
    state.row['baseInfo'].update(sellerAlipayId='2088000000000002', status='success')
    assert source.candidate(state.row) is None
    assert len(list(source.list_window(datetime(2026, 9, 23), datetime(2026, 9, 23, 1)))) == 1
    with pytest.raises(ValueError, match='商家身份'):
        source.refresh(SN)
    state.row['baseInfo']['status'] = 'waitbuyerreceive'
    with pytest.raises(ValueError, match='商家身份'):
        source.candidate(state.row)
    with pytest.raises(ValueError, match='商家身份'):
        list(source.list_window(datetime(2026, 9, 23), datetime(2026, 9, 23, 1)))


@pytest.mark.parametrize('status', ['success', 'cancel', 'terminated',
                                  'confirm_goods', 'confirm_goods_but_not_fund'])
def test_closed_order_excluded_without_claiming_money(setup, status):
    source, state = setup
    state.row['baseInfo']['status'] = status
    del state.row['nativeLogistics']
    result = source.refresh(SN)
    assert result == [] and result.closed['order_state'] == status
    assert result.full_refund is None
    assert source.candidate(state.row) is None


def test_full_refund_requires_consistent_actual_fields(setup):
    source, state = setup
    state.row['baseInfo'].update(refund=20, refundPayment=2000)
    result = source.refresh(SN)
    assert result == [] and result.full_refund['refund_amount'] == '20'
    state.row['baseInfo']['refundPayment'] = 1999
    with pytest.raises(ValueError, match='金额'):
        source.refresh(SN)


@pytest.mark.parametrize('field,value', [('id', 1), ('sellerAlipayId', '2088000000000002'),
    ('sellerAlipayId', '2088****'), ('status', 'newstate'), ('refund', None),
    ('refundPayment', None), ('refundPayment', 0.1), ('refund', 21),
    ('totalAmount', 'NaN'), ('totalAmount', 0)])
def test_identity_money_state_fail_closed(setup, field, value):
    source, state = setup
    state.row['baseInfo'][field] = value
    with pytest.raises(ValueError):
        source.refresh(SN)


@pytest.mark.parametrize('field,value', [('deliveredTime', None), ('deliveredTime', 'bad'),
    ('subItemIds', ''), ('subItemIds', '999'), ('subItemIds', '801,801'),
    ('logisticsBillNo', '不需要物流'), ('logisticsCompanyName', None), ('type', '2'),
    ('status', 'unknown')])
def test_malformed_package_fails_closed(setup, field, value):
    source, state = setup
    state.row['nativeLogistics']['logisticsItems'][0][field] = value
    with pytest.raises(ValueError):
        source.refresh(SN)


def test_duplicate_package_and_child_identity_rejected(setup):
    source, state = setup
    state.row['nativeLogistics']['logisticsItems'] *= 2
    with pytest.raises(ValueError, match='关联'):
        source.refresh(SN)
    state.row = trade()
    state.row['productItems'] *= 2
    with pytest.raises(ValueError, match='子单'):
        source.refresh(SN)


def test_cancelled_item_only_parcel_not_reminded(setup):
    source, state = setup
    state.row['productItems'][0]['status'] = 'cancel'
    assert source.refresh(SN) == []


def test_unshipped_has_no_fake_shipment_time(setup):
    source, state = setup
    state.row['baseInfo']['status'] = 'waitsellersend'
    state.row['productItems'][0].update(status='waitsellersend', logisticsStatus=1)
    del state.row['nativeLogistics']['logisticsItems']
    assert source.candidate(state.row) is None
    assert source.refresh(SN) == []


def test_list_empty_explicit_and_window_time(setup):
    source, state = setup
    state.bodies = [{'success': True, 'result': [], 'totalRecord': 0}]
    assert list(source.list_window(datetime(2026, 9, 23, 4), datetime(2026, 9, 23, 10))) == [[]]
    params = state.calls[-1][2]
    assert params['modifyStartTime'] == '20260923120000000+0800'
    assert 'orderStatus' not in params  # Includes changed terminal and partially shipped orders.
    with pytest.raises(ValueError):
        list(source.list_window(datetime(2026, 9, 23), datetime(2026, 9, 25)))


@pytest.mark.parametrize('body', [
    {'success': False, 'result': [], 'totalRecord': 0},
    {'success': True, 'totalRecord': 0},
    {'success': True, 'result': None, 'totalRecord': 0},
    {'success': True, 'result': [], 'totalRecord': 1},
    {'success': True, 'result': [], 'totalRecord': True},
    {'success': True, 'result': [], 'totalRecord': 0, 'retCodes': ['ERROR']},
])
def test_list_invalid_response_not_empty_success(setup, body):
    source, state = setup
    state.bodies = [body]
    with pytest.raises(ValueError):
        list(source.list_window(datetime(2026, 9, 23), datetime(2026, 9, 23, 1)))


@pytest.mark.parametrize('mode', ['repeat', 'changed_total', 'short_page'])
def test_list_incomplete_pagination(setup, mode):
    source, state = setup
    rows = []
    for i in range(21):
        r = trade()
        r['baseInfo'].update(id=1000+i, idOfStr=str(1000+i))
        rows.append(r)
    first = rows[:20] if mode != 'short_page' else rows[:19]
    last = rows[:1] if mode == 'repeat' else rows[20:]
    state.bodies = [{'success': True, 'result': first, 'totalRecord': 21},
                    {'success': True, 'result': last,
                     'totalRecord': 22 if mode == 'changed_total' else 21}]
    with pytest.raises(ValueError):
        list(source.list_window(datetime(2026, 9, 23), datetime(2026, 9, 23, 1)))


@pytest.mark.parametrize('api', ['alibaba.trade.refund.sellerAgreeRefund',
                                 'alibaba.trade.sendGoods', 'alibaba.trade.memoAdd'])
def test_read_client_rejects_business_writes(api):
    client = object.__new__(Alibaba1688ShipmentClient)
    with pytest.raises(ValueError, match='只读'):
        client.execute_read('com.alibaba.trade', api)


@pytest.fixture
def watch_case():
    yield from base.setup.__wrapped__()


@pytest.mark.parametrize('seconds,expected', [(71999, 0), (72000, 1), (75600, 1)])
def test_watch_boundary_dedup_and_trace_receipt_preservation(watch_case, seconds, expected):
    watch, session, order, _, state, _ = watch_case
    row = trade()
    shipped = base.NOW.replace(tzinfo=UTC) - timedelta(seconds=seconds)
    row['nativeLogistics']['logisticsItems'][0]['deliveredTime'] = shipped.astimezone(
        timezone_cn()).strftime('%Y%m%d%H%M%S000+0800')
    client = Mock()
    client.get_order_detail.side_effect = lambda *_: {'success': 'true', 'result': row}
    source = Alibaba1688ShipmentSource(client, seller_fingerprint=FINGERPRINT)
    order.order_sn, order.shop_code = SN, '1688-test'
    session.commit()
    watch.check_order(order, source, '合成1688店', publish=True)
    watch.check_order(order, source, '合成1688店', publish=True)
    assert state.posts == expected
    if expected:
        notice = session.scalar(select(base.Notice))
        assert notice.payload['source'] == '1688'
        receipt = notice.todo_id
        state.trace = True
        watch.check_order(order, source, '合成1688店', publish=True)
        assert notice.status == 'SENT' and notice.todo_id == receipt
        assert notice.payload['trace_resolved'] and state.posts == 1


def timezone_cn():
    from datetime import timezone
    return timezone(timedelta(hours=8))
