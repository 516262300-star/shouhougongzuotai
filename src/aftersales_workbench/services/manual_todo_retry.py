"""人工待办归属重查的等待时间；不代表发送授权或归属已确认。"""

from datetime import UTC, datetime


def owner_retry_waiting(payload, *, now=None):
    value = (payload or {}).get("owner_routing_retry_after")
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        due = datetime.fromisoformat(value)
    except ValueError:
        return False  # 旧记录格式异常时重新核验归属，不能据此直接发送。
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return due > (now or datetime.now(UTC))


def preserve_owner_retry(previous, refreshed):
    """更新业务文案时保留尚未到期的归属重查，不携带旧收件人或发送凭证。"""
    if not owner_retry_waiting(previous):
        return refreshed
    return {
        **refreshed,
        "owner_routing_retry_after": previous["owner_routing_retry_after"],
        "owner_routing_status": previous.get("owner_routing_status", "UNAVAILABLE"),
    }
