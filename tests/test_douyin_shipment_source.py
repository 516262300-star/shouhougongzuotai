from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from aftersales_workbench.workflows.douyin_orders import cents
from aftersales_workbench.workflows.douyin_shipment_source import DouyinShipmentSource

NOW = datetime(2026, 9, 24, 8)
SN = "1234567890123456789"


def trade():
    return dict(
        order_id=SN,
        shop_id="101",
        order_status=3,
        pay_amount=2000,
        sku_order_list=[dict(order_id="1001", after_sale_info={"refund_status": 0})],
        logistics_info=[
            dict(
                tracking_no="JT12345",
                company="jtexpress",
                ship_time=1790100000,
                product_info=[{"sku_order_id": "1001"}],
            )
        ],
    )


@pytest.fixture
def case():
    row, client = trade(), Mock()
    client.get_order_detail.side_effect = lambda *_: {"data": {"shop_order_detail": row}}
    return row, client, DouyinShipmentSource(client, shop_id="101")


def test_real_parcel_timestamp_and_identity(case):
    row, client, source = case
    parcels = source.refresh(SN)
    assert parcels[0].tracking_number == "JT12345" and parcels[0].carrier == "jtexpress"
    assert parcels[0].sub_order_ids == ("1001",)
    client.order_refunds.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("shop_id", "wrong"),
        ("order_id", "wrong"),
        ("order_status", 999),
        ("logistics_info", None),
        ("logistics_info", []),
    ],
)
def test_invalid_identity_or_missing_parcels_fail_closed(case, field, value):
    row, client, source = case
    row[field] = value
    with pytest.raises(ValueError):
        source.refresh(SN)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ship_time", None),
        ("ship_time", 0),
        ("tracking_no", ""),
        ("company", "123"),
        ("product_info", []),
        ("product_info", [{"sku_order_id": "other"}]),
    ],
)
def test_incomplete_parcel_cannot_be_reminded(case, field, value):
    row, client, source = case
    row["logistics_info"][0][field] = value
    with pytest.raises(ValueError):
        source.refresh(SN)


def test_partial_shipment_uses_each_parcel_own_time(case):
    row, client, source = case
    row["order_status"] = 2
    another = deepcopy(row["logistics_info"][0])
    another.update(tracking_no="JT54321", ship_time=1790103600)
    row["logistics_info"].append(another)
    p = source.refresh(SN)
    assert p[1].shipped_at - p[0].shipped_at == timedelta(hours=1)


@pytest.mark.parametrize("status", [4, 5])
def test_closed_and_completed_do_not_send_reminders(case, status):
    row, client, source = case
    row["order_status"] = status
    result = source.refresh(SN)
    assert result == [] and result.closed["platform"] == "DOUYIN"
    client.order_refunds.assert_not_called()


def refund(client, amount=2000, success=True):
    rows = [{"aftersale_info": {"aftersale_id": "9001"}}]
    client.order_refunds.side_effect = lambda *_: iter(rows)
    client.get_detail.return_value = {
        "data": {
            "order_info": {"shop_order_id": SN},
            "process_info": {
                "after_sale_info": {
                    "after_sale_id": "9001",
                    "after_sale_type": 0,
                    "refund_status": 3 if success else 1,
                    "refund_time": 1790200000,
                    "real_refund_amount": amount,
                }
            },
        }
    }


def test_full_refund_uses_actual_success_not_application_amount(case):
    row, client, source = case
    row["sku_order_list"][0]["after_sale_info"]["refund_status"] = 3
    refund(client)
    result = source.refresh(SN)
    assert result == [] and result.full_refund["refund_amount"] == "20"
    client.get_detail.return_value["data"]["process_info"]["after_sale_info"]["refund_status"] = 1
    with pytest.raises(ValueError):
        source.refresh(SN)


def test_partial_refund_keeps_remaining_order_monitored(case):
    row, client, source = case
    row["sku_order_list"][0]["after_sale_info"]["refund_status"] = 1
    refund(client, amount=500)
    assert len(source.refresh(SN)) == 1


@pytest.mark.parametrize("amount", [None, -1, 2001, 1.5])
def test_invalid_refund_totals_block_reminders(case, amount):
    row, client, source = case
    row["sku_order_list"][0]["after_sale_info"]["refund_status"] = 3
    refund(client, amount)
    with pytest.raises(ValueError):
        source.refresh(SN)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": {}},
        {"data": {"total": 1, "shop_order_list": []}},
        {"data": {"total": 101, "shop_order_list": [trade()]}},
        {"data": {"total": 2, "shop_order_list": [trade(), trade()]}},
    ],
)
def test_pagination_failures_never_advance_window(case, body):
    row, client, source = case
    client.execute_read.return_value = body
    with pytest.raises(ValueError):
        list(source.list_window(NOW - timedelta(hours=1), NOW))


def test_explicit_empty_page_and_zero_based_pagination(case):
    row, client, source = case
    client.execute_read.return_value = {"data": {"total": 0, "shop_order_list": []}}
    assert list(source.list_window(NOW - timedelta(hours=1), NOW)) == [[]]
    path, params = client.execute_read.call_args.args
    assert path == "/order/searchList" and params["page"] == 0


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", -1, 1.5])
def test_amount_must_be_explicit_integer_cents(value):
    with pytest.raises(ValueError):
        cents(value)
