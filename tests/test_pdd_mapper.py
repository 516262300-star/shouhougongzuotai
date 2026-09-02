from datetime import datetime
from decimal import Decimal

import pytest

from aftersales_workbench.db.models import AfterSalesType, ShippingStatus
from aftersales_workbench.integrations.pdd.mapper import (
    PddDataMappingError,
    normalize_refund,
    unwrap_order_information,
)


def test_normalize_refund_maps_real_pdd_shapes() -> None:
    list_record = {
        "id": 123,
        "order_sn": "order-1",
        "after_sales_type": 3,
        "refund_amount": "278.57",
        "goods_number": "22",
        "outer_id": "sku-fallback",
        "after_sale_reason": "reason",
        "tracking_number": "return-list-no",
        "after_sales_status": 10,
        "speed_refund_flag": 1,
        "goods_name": "6050 柜门拉手",
        "created_time": 1788192000,
        "updated_time": 1788195600,
    }
    detail = {
        "id": 123,
        "order_sn": "order-1",
        "after_sales_type": 2,
        "refund_amount": 27857,
        "order_amount": 27857,
        "goods_number": 22,
        "out_sku_sn": "sku-1",
        "after_sales_reason": "reason-detail",
        "remark": "memo",
        "express_no": "return-no",
        "shipping_name": "carrier",
    }
    order = {
        "order_status": 2,
        "goods_amount": 300.00,
        "platform_discount": 10.00,
        "seller_discount": 11.43,
        "tracking_number": "forward-no",
        "shipping_time": "2026-08-25 18:49:12",
        "logistics_id": 384,
        "refund_status": 4,
    }

    result = normalize_refund(list_record, detail, order)

    assert result.after_sales_sn == "123"
    assert result.after_sales_type is AfterSalesType.RETURN_AND_REFUND
    assert result.refund_amount == Decimal("278.57")
    assert result.platform_order_amount == Decimal("278.57")
    assert result.platform_goods_amount == Decimal("300.00")
    assert result.platform_discount_amount == Decimal("10.00")
    assert result.seller_discount_amount == Decimal("11.43")
    assert result.merchant_receivable_amount == Decimal("288.57")
    assert result.order_shipping_status is ShippingStatus.IN_TRANSIT
    assert result.forward_tracking_number == "forward-no"
    assert result.carrier_code == "384"
    assert result.return_tracking_number == "return-no"
    assert result.platform_after_sales_status == 10
    assert result.platform_order_refund_status == 4
    assert result.is_speed_refund is True
    assert result.product_name == "6050 柜门拉手"
    assert result.platform_created_at == datetime(2026, 9, 1, 0, 0)
    assert result.platform_updated_at == datetime(2026, 9, 1, 1, 0)
    assert result.item.sku_code == "sku-1"
    assert result.item.applied_quantity == 22


def test_normalize_refund_accepts_formatted_platform_times() -> None:
    result = normalize_refund(
        {
            "id": 456,
            "order_sn": "order-formatted-time",
            "after_sales_type": 2,
            "refund_amount": "4.94",
            "goods_number": 1,
            "outer_id": "sku-1",
            "created_time": "2026-09-02 10:38:32",
            "updated_time": "2026-09-02T10:41:50+08:00",
        },
        {
            "id": 456,
            "order_sn": "order-formatted-time",
            "after_sales_type": 1,
            "refund_amount": 494,
            "order_amount": 494,
            "out_sku_sn": "sku-1",
            "goods_number": 1,
        },
        {"order_status": 2, "tracking_number": "JT-test"},
    )

    assert result.platform_created_at == datetime(2026, 9, 2, 10, 38, 32)
    assert result.platform_updated_at == datetime(2026, 9, 2, 10, 41, 50)


def test_unknown_after_sales_type_is_rejected() -> None:
    with pytest.raises(PddDataMappingError, match="after_sales_type"):
        normalize_refund(
            {"id": 1, "order_sn": "order", "refund_amount": "1.00", "goods_number": 1},
            {"after_sales_type": 99, "refund_amount": 100, "out_sku_sn": "sku"},
            {"order_status": 1},
        )


def test_detail_type_is_used_only_when_list_type_is_missing() -> None:
    result = normalize_refund(
        {
            "id": 1,
            "order_sn": "order",
            "refund_amount": "1.00",
            "goods_number": 1,
        },
        {
            "after_sales_type": 1,
            "refund_amount": 100,
            "out_sku_sn": "sku",
        },
        {"order_status": 1},
    )

    assert result.after_sales_type is AfterSalesType.ONLY_REFUND


def test_platform_order_amount_falls_back_to_order_pay_amount() -> None:
    result = normalize_refund(
        {
            "id": 1,
            "order_sn": "order",
            "refund_amount": "1.00",
            "goods_number": 1,
        },
        {
            "after_sales_type": 1,
            "refund_amount": 100,
            "out_sku_sn": "sku",
        },
        {"order_status": 1, "pay_amount": 2.48},
    )

    assert result.platform_order_amount == Decimal("2.48")
    assert result.merchant_receivable_amount is None


def test_platform_coupon_is_added_back_to_merchant_receivable() -> None:
    result = normalize_refund(
        {
            "id": 1,
            "order_sn": "order",
            "after_sales_type": 2,
            "refund_amount": "1.88",
            "goods_number": 1,
        },
        {
            "id": 1,
            "order_sn": "order",
            "after_sales_type": 1,
            "refund_amount": 188,
            "order_amount": 188,
            "out_sku_sn": "sku",
            "goods_number": 1,
        },
        {
            "order_status": 2,
            "goods_amount": 3.88,
            "pay_amount": 1.88,
            "platform_discount": 1.00,
            "seller_discount": 1.00,
        },
    )

    assert result.refund_amount == Decimal("1.88")
    assert result.platform_order_amount == Decimal("1.88")
    assert result.platform_discount_amount == Decimal("1.00")
    assert result.seller_discount_amount == Decimal("1.00")
    assert result.merchant_receivable_amount == Decimal("2.88")


def test_unwrap_order_information_requires_nested_order() -> None:
    assert unwrap_order_information(
        {"order_info_get_response": {"order_info": {"order_status": 1}}}
    ) == {"order_status": 1}

    with pytest.raises(PddDataMappingError, match="order_info"):
        unwrap_order_information({"order_info_get_response": {}})
