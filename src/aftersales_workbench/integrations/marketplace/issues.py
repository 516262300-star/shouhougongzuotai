from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import MarketplaceSyncIssue
from aftersales_workbench.workflows.polling import utcnow


class SyncIssueRepository:
    def __init__(self, session: Session):
        self.session = session

    def record(
        self, shop_id: int, refund_id: str, error: str,
        *, platform_order_sn: str | None = None,
    ) -> None:
        row = self.session.get(MarketplaceSyncIssue, (shop_id, refund_id))
        if row is not None and row.dismissed_at is not None:
            return  # 明确忽略后，重复窗口/旧重查结果不能重新报警或覆盖原始证据。
        now = utcnow()
        if row is None:
            row = MarketplaceSyncIssue(shop_id=shop_id, after_sales_sn=refund_id, attempts=0)
            self.session.add(row)
        if platform_order_sn is not None:
            row.platform_order_sn = platform_order_sn
        row.attempts += 1
        row.last_error = error[:500]
        row.checked_at = now
        row.next_retry_at = now + timedelta(hours=min(24, 2 ** min(row.attempts - 1, 5)))
        row.resolved_at = None
        self.session.flush()

    def resolve(self, shop_id: int, refund_id: str) -> bool:
        row = self.session.get(MarketplaceSyncIssue, (shop_id, refund_id))
        if row is None or row.resolved_at is not None or row.dismissed_at is not None:
            return False
        row.resolved_at = utcnow()
        return True

    def is_dismissed(self, shop_id: int, refund_id: str) -> bool:
        return self.session.scalar(select(MarketplaceSyncIssue.dismissed_at).where(
            MarketplaceSyncIssue.shop_id == shop_id,
            MarketplaceSyncIssue.after_sales_sn == refund_id,
        )) is not None

    def dismiss(self, shop_id: int, refund_id: str, *, order_sn: str, reason: str) -> bool:
        """仅移除提醒/重查，不伪造同步恢复，不解除资金隔离；调用方负责提交。"""
        reason = reason.strip()
        if not reason or len(reason) > 500 or not order_sn.strip():
            raise ValueError("必须提供完整平台订单号和不超过500字的明确处置原因")
        row = self.session.scalar(select(MarketplaceSyncIssue).where(
            MarketplaceSyncIssue.shop_id == shop_id,
            MarketplaceSyncIssue.after_sales_sn == refund_id,
        ).with_for_update().execution_options(populate_existing=True))
        if row is None or row.platform_order_sn != order_sn:
            raise ValueError("店铺、售后号和平台订单号未精确匹配，禁止移除")
        if row.dismissed_at is not None:
            return False
        if row.resolved_at is not None:
            raise ValueError("这笔异常已正常恢复，不再移除")
        row.dismissed_at = utcnow()
        row.dismissed_reason = reason
        self.session.flush()
        return True

    def due(self, shop_id: int, limit: int = 20) -> list[str]:
        return list(
            self.session.scalars(
                select(MarketplaceSyncIssue.after_sales_sn)
                .where(
                    MarketplaceSyncIssue.shop_id == shop_id,
                    MarketplaceSyncIssue.resolved_at.is_(None),
                    MarketplaceSyncIssue.dismissed_at.is_(None),
                    MarketplaceSyncIssue.next_retry_at <= utcnow(),
                )
                .order_by(MarketplaceSyncIssue.next_retry_at, MarketplaceSyncIssue.after_sales_sn)
                .limit(limit)
            )
        )

    def outstanding(self, shop_id: int) -> int:
        self.session.flush()
        return (
            self.session.scalar(
                select(func.count())
                .select_from(MarketplaceSyncIssue)
                .where(
                    MarketplaceSyncIssue.shop_id == shop_id,
                    MarketplaceSyncIssue.resolved_at.is_(None),
                    MarketplaceSyncIssue.dismissed_at.is_(None),
                )
            )
            or 0
        )
