"""工作台只读展示口径；不据此发起退款、认领或 ERP 写入。"""

from typing import Any

from sqlalchemy import and_, exists, func, or_

from aftersales_workbench.db.models import AfterSalesOrder, Platform, Shop


def confirmed_refund(order: AfterSalesOrder, platform: Platform | str) -> bool:
    # 10/4 仅为拼多多代码，不套用到其他平台。
    return str(order.refund_financial_status or "").upper() == "SUCCESS" or (
        platform == Platform.PDD
        and (order.platform_after_sales_status == 10 or order.platform_order_refund_status == 4)
    )


def confirmed_refund_filter() -> Any:
    pdd_shop = exists().where(
        Shop.shop_id == AfterSalesOrder.shop_id,
        Shop.platform == Platform.PDD,
    ).correlate(AfterSalesOrder)
    return or_(
        func.upper(func.coalesce(AfterSalesOrder.refund_financial_status, "")) == "SUCCESS",
        and_(
            pdd_shop,
            or_(
                func.coalesce(AfterSalesOrder.platform_after_sales_status, 0) == 10,
                func.coalesce(AfterSalesOrder.platform_order_refund_status, 0) == 4,
            ),
        ),
    )


def refund_display(
    order: AfterSalesOrder, platform: Platform | str, *, submitted: bool = False
) -> dict[str, str]:
    status = str(order.refund_financial_status or "UNKNOWN").upper()
    if confirmed_refund(order, platform):
        status, label, tone, reason = (
            "SUCCESS", "平台已退款", "success",
            "平台已明确返回退款成功；不代表退货已到仓或 ERP 已平账。",
        )
    elif status == "CLOSED":
        status, label, tone, reason = (
            "CLOSED", "退款已关闭", "neutral", "平台退款申请已关闭，未确认退款成功。",
        )
    elif submitted:
        status, label, tone, reason = (
            "SUBMITTED", "已提交·待平台确认", "info",
            "同意退款任务已完成，但尚未同步到平台明确成功结果；不会因展示待确认而重发退款。",
        )
    elif status == "PENDING":
        status, label, tone, reason = (
            "PENDING", "待平台退款", "info", "尚未同步到平台明确退款成功结果。",
        )
    else:
        status, label, tone, reason = (
            "UNKNOWN", "平台状态待确认", "warning",
            "平台状态缺失或无法识别，不能仅凭验货通过或本地流程状态认定已退款。",
        )
    return {"status": status, "label": label, "tone": tone, "reason": reason}
