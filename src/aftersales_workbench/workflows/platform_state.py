from __future__ import annotations

from aftersales_workbench.db.models import AfterSalesOrder


def platform_refund_completed(order: AfterSalesOrder) -> bool:
    """跨平台判断退款是否已经明确成功，未知状态一律不猜测。"""
    return (
        str(getattr(order, "refund_financial_status", "") or "").upper()
        == "SUCCESS"
        or order.platform_after_sales_status == 10
        or order.platform_order_refund_status == 4
    )
