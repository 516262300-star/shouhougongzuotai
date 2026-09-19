from copy import deepcopy
from types import SimpleNamespace

import pytest

from aftersales_workbench.integrations.tmall.client import TmallClient
from aftersales_workbench.workflows.shipment_refund import pdd_full_refund, tmall_full_refund
from aftersales_workbench.workflows.shipment_watch_sources import ShipmentSource


def pdd(refund_cents=1858):
    order = {"order_sn": "pdd-1", "order_status": 2, "refund_status": 4,
             "pay_amount": "18.58", "shipping_time": "2026-09-18 18:51:15",
             "tracking_number": "track-1", "logistics_id": 384}
    refund = {"order_sn": "pdd-1", "id": 123, "after_sales_status": 10,
              "refund_amount": refund_cents}
    client = SimpleNamespace(
        get_order_information=lambda **kw: {"order_info_get_response": {"order_info": order}},
        get_refund_information=lambda **kw: refund,
    )
    return client, order, refund


@pytest.mark.parametrize("cents,excluded", [(1858, True), (100, False)])
def test_pdd_full_refund_uses_success_and_matching_paid_amount(cents, excluded):
    client, _, _ = pdd(cents)
    snapshot = ShipmentSource("PDD", client).refresh("pdd-1")
    assert bool(getattr(snapshot, "full_refund", None)) == excluded
    assert len(snapshot) == (0 if excluded else 1)


@pytest.mark.parametrize("field,value", [
    ("order_sn", "another"), ("id", None), ("after_sales_status", 2),
    ("refund_amount", None), ("refund_amount", "NaN"), ("refund_amount", 1900),
])
def test_pdd_incomplete_or_conflicting_refund_proof_blocks_reminder(field, value):
    client, order, refund = pdd()
    refund[field] = value
    with pytest.raises(ValueError):
        pdd_full_refund(client, order)


def test_pdd_pending_refund_is_still_monitored_and_failed_read_is_not_no_refund():
    client, order, _ = pdd()
    def fail(**kw):
        raise TimeoutError("平台暂不可读")
    client.get_refund_information = fail
    order["refund_status"] = 2
    assert len(ShipmentSource("PDD", client).refresh("pdd-1")) == 1
    order["refund_status"] = 4
    with pytest.raises(TimeoutError):
        ShipmentSource("PDD", client).refresh("pdd-1")


def tmall():
    trade = {"tid": 123, "status": "WAIT_BUYER_CONFIRM_GOODS", "payment": "30.00",
             "consign_time": "2026-09-18 18:00:00", "orders": {"order": [
                 {"oid": 1, "refund_id": 11, "refund_status": "SUCCESS"},
                 {"oid": 2, "refund_id": 12, "refund_status": "SUCCESS"},
             ]}}
    refunds = {11: {"tid": 123, "oid": 1, "refund_id": 11, "status": "SUCCESS",
                    "refund_fee": "10.00"},
               12: {"tid": 123, "oid": 2, "refund_id": 12, "status": "SUCCESS",
                    "refund_fee": "20.00"}}
    client = SimpleNamespace(
        get_trade_fullinfo=lambda **kw: {"trade_fullinfo_get_response": {"trade": trade}},
        get_refund=lambda refund_id: {"refund_get_response": {"refund": refunds[refund_id]}},
        _refund_from_response=TmallClient._refund_from_response,
    )
    return client, trade, refunds


def test_tmall_full_refund_requires_all_suborders_and_total_paid_amount():
    client, trade, refunds = tmall()
    snapshot = ShipmentSource("TMALL", client).refresh("123")
    assert not snapshot and snapshot.full_refund["refund_ids"] == ["11", "12"]
    refunds[12]["refund_fee"] = "19.00"
    assert tmall_full_refund(client, trade) is None
    trade["orders"]["order"][1]["refund_status"] = "NO_REFUND"
    assert tmall_full_refund(client, trade) is None


@pytest.mark.parametrize("field,value", [
    ("tid", 999), ("oid", 999), ("refund_id", 999),
    ("status", "WAIT_SELLER_AGREE"), ("refund_fee", "NaN"), ("refund_fee", "21.00"),
])
def test_tmall_refund_proof_identity_status_and_amount_must_match(field, value):
    client, trade, refunds = tmall()
    refunds[12][field] = value
    with pytest.raises(ValueError):
        tmall_full_refund(client, trade)


def test_tmall_duplicate_suborders_and_refunds_cannot_be_double_counted():
    client, trade, _ = tmall()
    trade["orders"]["order"][1] = deepcopy(trade["orders"]["order"][0])
    with pytest.raises(ValueError, match="重复"):
        tmall_full_refund(client, trade)
