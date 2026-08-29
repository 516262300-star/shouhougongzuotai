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
    }
    detail = {
        "id": 123,
        "order_sn": "order-1",
        "after_sales_type": 2,
        "refund_amount": 27857,
        "goods_number": 22,
        "out_sku_sn": "sku-1",
        "after_sales_reason": "reason-detail",
        "remark": "memo",
        "express_no": "return-no",
        "shipping_name": "carrier",
    }
    order = {
        "order_status": 2,
        "tracking_number": "forward-no",
        "shipping_time": "2026-08-25 18:49:12",
        "logistics_id": 384,
    }

    result = normalize_refund(list_record, detail, order)

    assert result.after_sales_sn == "123"
    assert result.after_sales_type is AfterSalesType.RETURN_AND_REFUND
    assert result.refund_amount == Decimal("278.57")
    assert result.order_shipping_status is ShippingStatus.IN_TRANSIT
    assert result.forward_tracking_number == "forward-no"
    assert result.carrier_code == "384"
    assert result.return_tracking_number == "return-no"
    assert result.item.sku_code == "sku-1"
    assert result.item.applied_quantity == 22


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


def test_unwrap_order_information_requires_nested_order() -> None:
    assert unwrap_order_information(
        {"order_info_get_response": {"order_info": {"order_status": 1}}}
    ) == {"order_status": 1}

    with pytest.raises(PddDataMappingError, match="order_info"):
        unwrap_order_information({"order_info_get_response": {}})
