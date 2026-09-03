from __future__ import annotations

from decimal import Decimal

from aftersales_workbench.db.models import AfterSalesType, ShippingStatus
from aftersales_workbench.integrations.tmall.mapper import normalize_refund


def test_normalize_return_refund_uses_trade_sku_and_return_tracking() -> None:
    record = {
        "refund_id": "9001",
        "tid": 8001,
        "oid": 7001,
        "refund_fee": "12.30",
        "payment": "12.30",
        "total_fee": "15.00",
        "has_good_return": True,
        "reason": "质量问题",
        "desc": "表面有划痕",
        "title": "测试拉手",
        "num": 2,
        "created": "2026-09-01 10:00:00",
        "modified": "2026-09-01 10:05:00",
        "order_status": "WAIT_BUYER_CONFIRM_GOODS",
        "status": "SUCCESS",
        "sid": "YT123",
        "company_name": "圆通速递",
    }
    detail = dict(record)
    trade = {
        "status": "WAIT_BUYER_CONFIRM_GOODS",
        "orders": {
            "order": [
                {
                    "oid": 7001,
                    "outer_sku_id": "8064-25#铜本色",
                    "title": "测试拉手",
                    "num": 2,
                }
            ]
        },
    }

    refund = normalize_refund(record, detail, trade)

    assert refund.after_sales_type is AfterSalesType.RETURN_AND_REFUND
    assert refund.refund_amount == Decimal("12.30")
    assert refund.order_shipping_status is ShippingStatus.IN_TRANSIT
    assert refund.return_tracking_number == "YT123"
    assert refund.platform_after_sales_status_text == "SUCCESS"
    assert refund.platform_order_status_text == "WAIT_BUYER_CONFIRM_GOODS"
    assert refund.item.sku_code == "8064-25#铜本色"
    assert refund.item.applied_quantity == 2


def test_normalize_only_refund_without_shipping_is_unshipped() -> None:
    record = {
        "refund_id": "9002",
        "tid": 8002,
        "oid": 7002,
        "refund_fee": "1.00",
        "has_good_return": False,
        "num": 1,
        "order_status": "WAIT_SELLER_SEND_GOODS",
    }

    refund = normalize_refund(record, record, {"status": "WAIT_SELLER_SEND_GOODS"})

    assert refund.after_sales_type is AfterSalesType.ONLY_REFUND
    assert refund.order_shipping_status is ShippingStatus.UNSHIPPED
    assert refund.item.sku_code == "7002"


def test_normalize_only_refund_uses_unique_forward_logistics_package() -> None:
    record = {
        "refund_id": "9003",
        "tid": 8003,
        "oid": 7003,
        "refund_fee": "19.90",
        "payment": "19.90",
        "has_good_return": False,
        "num": 1,
        "order_status": "WAIT_BUYER_CONFIRM_GOODS",
        "status": "WAIT_SELLER_AGREE",
    }
    logistics = {
        "logistics_orders_get_response": {
            "shippings": {
                "shipping": [
                    {
                        "out_sid": "JT123456",
                        "company_name": "极兔速递",
                        "status": "ACCEPTED_BY_RECEIVER",
                    }
                ]
            }
        }
    }

    refund = normalize_refund(record, record, record, logistics)

    assert refund.forward_tracking_number == "JT123456"
    assert refund.carrier_code == "极兔速递"
