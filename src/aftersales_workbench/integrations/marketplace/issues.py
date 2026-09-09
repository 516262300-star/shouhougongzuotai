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
        if row is None or row.resolved_at is not None:
            return False
        row.resolved_at = utcnow()
        return True

    def due(self, shop_id: int, limit: int = 20) -> list[str]:
        return list(
            self.session.scalars(
                select(MarketplaceSyncIssue.after_sales_sn)
                .where(
                    MarketplaceSyncIssue.shop_id == shop_id,
                    MarketplaceSyncIssue.resolved_at.is_(None),
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
                )
            )
            or 0
        )
