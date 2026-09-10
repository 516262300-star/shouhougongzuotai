"""资格通过时保存批准范围；同步变化不得静默扩大旧资金任务授权。"""


def refund_snapshot(order):
    return {
        "version": 1,
        "shop_id": getattr(order, "shop_id", None),
        "after_sales_sn": str(order.after_sales_sn),
        "platform_order_sn": str(order.platform_order_sn),
        "after_sales_type": str(order.after_sales_type),
        "refund_amount": str(order.refund_amount),
        "forward_tracking_number": getattr(order, "forward_tracking_number", None),
        "return_tracking_number": getattr(order, "return_tracking_number", None),
        "items": sorted(
            [
                [str(i.sku_code), str(getattr(i, "color", None) or ""), int(i.applied_quantity)]
                for i in getattr(order, "items", ())
            ]
        ),
    }


def require_refund_snapshot(order, payload):
    expected = payload.get("approval_snapshot")
    if not isinstance(expected, dict) or expected != refund_snapshot(order):
        raise ValueError("退款批准快照缺失或已变化，必须重新核验，禁止沿用旧任务退款")
