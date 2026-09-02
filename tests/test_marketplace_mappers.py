from __future__ import annotations

from decimal import Decimal

from aftersales_workbench.db.models import AfterSalesType, ShippingStatus
from aftersales_workbench.integrations.marketplace.alibaba_1688 import (
    normalize_1688_refund,
)
from aftersales_workbench.integrations.marketplace.douyin import (
    normalize_douyin_refund,
)
from aftersales_workbench.integrations.marketplace.jd import (
    normalize_jd_aftersale,
    normalize_jd_refund_apply,
)


def test_normalize_1688_return_refund() -> None:
    refund = normalize_1688_refund(
        {
            "refundId": "A-1",
            "orderId": "O-1",
            "applyPayment": 1230,
            "applyCarriage": 200,
            "refundGoods": True,
            "applyReason": "质量问题",
            "orderEntryCountMap": {"L-1": 2},
            "gmtApply": "20260901100000000+0800",
            "status": "waitsellerreceive",
            "freightBill": "RET-1",
        },
        {
            "baseInfo": {"status": "waitbuyerreceive", "totalAmount": "20.00"},
            "productItems": [
                {
                    "subItemID": "L-1",
                    "cargoNumber": "8064-25#铜本色",
                    "quantity": 2,
                    "itemAmount": "18.00",
                    "name": "拉手",
                }
            ],
        },
    )

    assert refund.refund_amount == Decimal("14.30")
    assert refund.after_sales_type is AfterSalesType.RETURN_AND_REFUND
    assert refund.order_shipping_status is ShippingStatus.IN_TRANSIT
    assert refund.items[0].sku_code == "8064-25#铜本色"


def test_normalize_jd_refund_and_return_service() -> None:
    order = {
        "orderPayment": "12.30",
        "orderSellerPrice": "15.00",
        "orderState": "WAIT_GOODS_RECEIVE_CONFIRM",
        "itemInfoList": [
            {
                "wareId": 10,
                "outerSkuId": "SKU-1",
                "itemTotal": 2,
                "wareName": "拉手",
            }
        ],
    }
    refund = normalize_jd_refund_apply(
        {"id": 100, "orderId": 200, "applyRefundSum": 1230, "status": 0},
        order,
    )
    returned = normalize_jd_aftersale(
        {"refoundAmount": 6.15, "status": 13},
        {"serviceId": 101, "orderId": 200, "wareId": 10, "serviceCount": 1},
        order,
    )

    assert refund.after_sales_type is AfterSalesType.ONLY_REFUND
    assert refund.refund_amount == Decimal("12.30")
    assert returned.after_sales_type is AfterSalesType.RETURN_AND_REFUND
    assert returned.items[0].sku_code == "SKU-1"


def test_normalize_douyin_refund_uses_detail_sku_and_return_tracking() -> None:
    refund = normalize_douyin_refund(
        {
            "aftersale_info": {
                "aftersale_id": "D-1",
                "related_id": "LINE-1",
                "aftersale_num": 1,
                "refund_amount": 188,
                "got_pkg": 1,
                "create_time": 1_788_230_400,
                "update_time": 1_788_234_000,
            },
            "order_info": {"shop_order_id": "DO-1", "pay_amount": 288},
            "text_part": {"reason_text": "不想要了", "aftersale_status_text": "待处理"},
        },
        {
            "order_info": {
                "sku_order_infos": [
                    {
                        "shop_sku_code": "SKU-DY",
                        "after_sale_item_count": 1,
                        "product_name": "拉手",
                    }
                ]
            },
            "process_info": {
                "logistics_info": {
                    "return": {"tracking_no": "RET-DY", "company_name": "圆通"}
                }
            },
        },
    )

    assert refund.refund_amount == Decimal("1.88")
    assert refund.platform_order_amount == Decimal("2.88")
    assert refund.return_tracking_number == "RET-DY"
    assert refund.items[0].sku_code == "SKU-DY"
