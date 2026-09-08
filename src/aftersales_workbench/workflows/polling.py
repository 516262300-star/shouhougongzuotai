from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, aliased

from aftersales_workbench.db.models import AutomationPollState


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def due_first(statement, *, scope: str, reference, tie_breaker, now=None):
    """在数据库 LIMIT 之前过滤未到期项，优先从未检查及最久未查的项。"""
    progress = aliased(AutomationPollState)
    return (
        statement.outerjoin(
            progress,
            and_(progress.scope == scope, progress.reference == reference),
        )
        .where(or_(progress.next_check_at.is_(None), progress.next_check_at <= (now or utcnow())))
        .order_by(None)
        .order_by(progress.checked_at.is_not(None), progress.checked_at, tie_breaker)
    )


def record_poll(
    session: Session,
    *,
    scope: str,
    reference: str,
    delay_seconds: int,
    error: str | None = None,
    now=None,
) -> None:
    checked = now or utcnow()
    row = session.get(AutomationPollState, (scope, reference))
    if row is None:
        row = AutomationPollState(scope=scope, reference=reference)
        session.add(row)
    row.checked_at = checked
    row.next_check_at = checked + timedelta(seconds=delay_seconds)
    row.last_error = error[:500] if error else None
