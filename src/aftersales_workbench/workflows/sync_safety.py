"""异常售后只阻止关联订单；正常店铺/订单不因其他记录异常停止。"""

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

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


def sync_safe_task_filter(pdd_shop_codes: tuple[str, ...] | None = None):
    return exists().where(
        AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
        sync_safe_order_filter(pdd_shop_codes),
    ).correlate(AftersalesActionTask)


def require_sync_safe_order(
    session: Session, after_sales_sn: str,
    pdd_shop_codes: tuple[str, ...] | None = None,
) -> None:
    allowed = session.scalar(select(AfterSalesOrder.id).where(
        AfterSalesOrder.after_sales_sn == after_sales_sn,
        sync_safe_order_filter(pdd_shop_codes),
    ))
    if allowed is None:
        raise ValueError("该订单存在未解决的同步异常，或本店当轮同步未成功，禁止自动执行")
