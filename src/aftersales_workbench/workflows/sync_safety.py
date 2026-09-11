"""异常售后只阻止关联订单；正常店铺/订单不因其他记录异常停止。"""

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    MarketplaceSyncIssue,
    Platform,
    Shop,
)


def sync_safe_order_filter(pdd_shop_codes: tuple[str, ...] | None = None):
    issue = exists().where(
        MarketplaceSyncIssue.shop_id == AfterSalesOrder.shop_id,
        MarketplaceSyncIssue.resolved_at.is_(None),
        or_(
            MarketplaceSyncIssue.after_sales_sn == AfterSalesOrder.after_sales_sn,
            MarketplaceSyncIssue.platform_order_sn == AfterSalesOrder.platform_order_sn,
        ),
    ).correlate(AfterSalesOrder)
    allowed = ~issue
    # None 为独立命令未提供当轮店铺范围；空元组明确禁止所有拼多多动作。
    if pdd_shop_codes is not None:
        allowed = and_(allowed, exists().where(
            Shop.shop_id == AfterSalesOrder.shop_id,
            or_(Shop.platform != Platform.PDD, Shop.shop_code.in_(pdd_shop_codes)),
        ).correlate(AfterSalesOrder))
    return allowed


def case_safe_order_filter():
    case_task = aliased(AftersalesActionTask)
    return ~exists().where(
        case_task.after_sales_sn == AfterSalesOrder.after_sales_sn,
        case_task.action_type == "PDD_AGREE_REFUND",
        case_task.action_status == "FAILED",
        case_task.payload["pdd_refund_case"]["code"].as_string().is_not(None),
    ).correlate(AfterSalesOrder)


def sync_safe_task_filter(pdd_shop_codes: tuple[str, ...] | None = None):
    return exists().where(
        AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
        sync_safe_order_filter(pdd_shop_codes),
        or_(AftersalesActionTask.action_type == "ERP_CREATE_MANUAL_TODO", case_safe_order_filter()),
    ).correlate(AftersalesActionTask)


def require_sync_safe_order(
    session: Session, after_sales_sn: str,
    pdd_shop_codes: tuple[str, ...] | None = None,
) -> None:
    allowed = session.scalar(select(AfterSalesOrder.id).where(
        AfterSalesOrder.after_sales_sn == after_sales_sn,
        sync_safe_order_filter(pdd_shop_codes),
        case_safe_order_filter(),
    ))
    if allowed is None:
        raise ValueError("该订单存在同步异常、售后类型待核验或本店同步未成功，禁止自动执行")
