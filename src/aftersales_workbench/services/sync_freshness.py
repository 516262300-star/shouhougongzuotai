"""同步水位的只读视图；订单业务字段更新时间不代表同步成功。"""

from datetime import UTC, datetime

from sqlalchemy import select

from aftersales_workbench.db.models import (
    PddSyncCursor,
    PlatformSyncCursor,
    Shop,
    TmallSyncCursor,
)


def sync_freshness(session, *, platform=None, shop_id=None):
    statement = select(Shop).where(Shop.is_active == 1)
    if platform:
        statement = statement.where(Shop.platform == platform)
    if shop_id is not None:
        statement = statement.where(Shop.shop_id == shop_id)
    shops = session.scalars(statement).all()
    ids = [shop.shop_id for shop in shops]
    cursors = {}
    for model in (PddSyncCursor, TmallSyncCursor, PlatformSyncCursor):
        if ids:
            for row in session.scalars(select(model).where(model.shop_id.in_(ids))):
                if model is PddSyncCursor and row.sync_scope != "refund-statuses:2,3,10":
                    continue
                if model is TmallSyncCursor and row.sync_scope != "refunds:all":
                    continue
                cursors[(row.shop_id, row.sync_scope)] = row
    rows = []
    for shop in shops:
        scope = (
            "refund-statuses:2,3,10"
            if shop.platform == "PDD"
            else "refunds:all"
            if shop.platform == "TMALL"
            else f"refunds:{str(shop.platform).lower()}"
        )
        row = cursors.get((shop.shop_id, scope))
        value = row.last_success_at if row else None
        rows.append(
            {
                "shop_id": shop.shop_id,
                "platform": str(shop.platform),
                "last_success_at": value.replace(tzinfo=UTC).isoformat() if value else None,
                "has_error": bool(row and row.last_error),
            }
        )
    times = [row["last_success_at"] for row in rows if row["last_success_at"]]
    return {
        "source": "committed_sync_cursor",
        "checked_at": datetime.now(UTC).isoformat(),
        "shop_count": len(rows),
        "missing_shop_count": len(rows) - len(times),
        "error_shop_count": sum(row["has_error"] for row in rows),
        "oldest_success_at": min(times) if times else None,
        "latest_success_at": max(times) if times else None,
        "shops": rows,
    }
