"""订单详情不可读的接入前已退款记录：保留证据、每日复查，不冒充平账。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    MarketplaceSyncIssue,
    Platform,
    Shop,
)
from aftersales_workbench.integrations.marketplace.issues import SyncIssueRepository
from aftersales_workbench.workflows.polling import utcnow

HISTORY_PREFIX = "PDD_HISTORY_V1:"


def is_history(row: MarketplaceSyncIssue | None) -> bool:
    return bool(
        row and row.dismissed_at and (row.dismissed_reason or "").startswith(HISTORY_PREFIX)
    )


def application_timestamp(value: Any) -> int | None:
    try:
        if str(value).isdigit():
            return int(value) if int(value) > 0 else None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(parsed.timestamp()) if parsed.timestamp() > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


class PddHistoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def reopen(self, shop_id: int, refund_id: str) -> bool:
        row = self.session.get(MarketplaceSyncIssue, (shop_id, refund_id))
        if is_history(row):
            # 保留上次分类证据；新状态/新错误重新受正常同步和资金闸门保护。
            row.dismissed_at = None
            row.resolved_at = None
            self.session.flush()
            return True
        return False

    def defer(
        self,
        shop_id: int,
        record: dict[str, Any],
        detail: dict[str, Any],
        *,
        now_at: int,
    ) -> bool:
        """仅在调用方确认订单详情 45001 后使用，不能吞掉鉴权/超时错误。"""
        refund_id = str(record.get("id") or "")
        order_sn = str(record.get("order_sn") or "")
        if not refund_id.isdigit() or not order_sn:
            return False
        if (
            str(detail.get("id") or "") != refund_id
            or str(detail.get("order_sn") or "") != order_sn
        ):
            return False
        try:
            codes = tuple(
                int(x)
                for x in (
                    record.get("after_sales_type"),
                    detail.get("after_sales_type"),
                    record.get("after_sales_status"),
                    detail.get("after_sales_status"),
                )
            )
        except (ValueError, TypeError):
            return False
        # 列表与详情的类型编码不同，必须成对核实；补寄/维修完成不等于退款。
        # 仅退款与退货退款均可保留为历史待核验，不代表仓库收货或 ERP 平账。
        if codes not in {(2, 1, 10, 10), (3, 2, 10, 10)}:
            return False
        try:
            cents = Decimal(str(detail.get("refund_amount")))
            yuan = Decimal(str(record.get("refund_amount")))
            if not cents.is_finite() or not yuan.is_finite() or cents < 0 or yuan * 100 != cents:
                return False
        except InvalidOperation:
            return False
        shop = self.session.get(Shop, shop_id)
        if not shop or shop.platform != Platform.PDD or not shop.created_at:
            return False
        # 接入日期前一天零点（上海时区）作为保守界线，避开旧数据时区偏差。
        cutoff = int(
            (
                datetime.combine(shop.created_at.date(), datetime.min.time()).replace(
                    tzinfo=timezone(timedelta(hours=8))
                )
                - timedelta(days=1)
            ).timestamp()
        )
        times = [
            application_timestamp(record.get("created_time")),
            application_timestamp(detail.get("recreated_at")),
        ]
        if any(t is None for t in times) or max(times) >= min(cutoff, now_at - 30 * 86400):
            return False
        # 任何已有业务跟进都不降为历史项，包括同平台订单的另一笔售后。
        if (
            self.session.scalar(
                select(AfterSalesOrder.id)
                .where(
                    or_(
                        AfterSalesOrder.after_sales_sn == refund_id,
                        AfterSalesOrder.platform_order_sn == order_sn,
                    )
                )
                .limit(1)
            )
            is not None
        ):
            return False
        if (
            self.session.scalar(
                select(AftersalesActionTask.id)
                .where(
                    AftersalesActionTask.after_sales_sn == refund_id,
                )
                .limit(1)
            )
            is not None
        ):
            return False
        row = self.session.get(MarketplaceSyncIssue, (shop_id, refund_id))
        if row and (
            row.platform_order_sn != order_sn or (row.dismissed_at and not is_history(row))
        ):
            return False
        if row is None:
            SyncIssueRepository(self.session).record(
                shop_id,
                refund_id,
                "PDD 45001：历史订单详情不可读",
                platform_order_sn=order_sn,
            )
            row = self.session.get(MarketplaceSyncIssue, (shop_id, refund_id))
        evidence = {
            "category": "历史已退款，资料待核验（不代表ERP平账）",
            "applied": times,
            "cutoff": cutoff,
            "codes": codes,
            "refund_cents": str(detail.get("refund_amount", ""))[:30],
            "updated": str(detail.get("updated_time", ""))[:40],
            "verified_at": now_at,
        }
        row.dismissed_reason = HISTORY_PREFIX + json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":")
        )
        row.dismissed_at = datetime.fromtimestamp(now_at, UTC).replace(tzinfo=None)
        row.next_retry_at = row.dismissed_at + timedelta(hours=24)
        row.resolved_at = None
        self.session.flush()
        return True

    def postpone_if_history(self, shop_id: int, refund_id: str) -> bool:
        row = self.session.get(MarketplaceSyncIssue, (shop_id, refund_id))
        if not is_history(row):
            return False
        # 精确查询未返回旧单不等于重新申请；保留证据，下一天再查。
        row.next_retry_at = utcnow() + timedelta(hours=24)
        self.session.flush()
        return True

    def due(self, shop_id: int, limit: int = 5) -> list[tuple[str, str]]:
        return [
            (row.after_sales_sn, row.platform_order_sn or "")
            for row in self.session.scalars(
                select(MarketplaceSyncIssue)
                .where(
                    MarketplaceSyncIssue.shop_id == shop_id,
                    MarketplaceSyncIssue.resolved_at.is_(None),
                    MarketplaceSyncIssue.dismissed_at.is_not(None),
                    MarketplaceSyncIssue.dismissed_reason.startswith(
                        HISTORY_PREFIX, autoescape=True
                    ),
                    MarketplaceSyncIssue.next_retry_at <= utcnow(),
                )
                .order_by(MarketplaceSyncIssue.next_retry_at, MarketplaceSyncIssue.after_sales_sn)
                .limit(limit)
            )
        ]
