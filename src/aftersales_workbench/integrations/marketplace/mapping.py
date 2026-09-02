from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any


def nonempty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def required_text(value: Any, *, field: str) -> str:
    text = nonempty(value)
    if not text:
        raise ValueError(f"缺少 {field}")
    return text


def money(value: Any, *, field: str, divisor: int = 1) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value)) / Decimal(divisor)
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError(f"{field} 不是有效金额") from exc
    if amount < 0:
        raise ValueError(f"{field} 不能小于 0")
    return amount


def positive_int(value: Any, *, field: str, default: int = 1) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        result = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是整数") from exc
    if result < 1:
        raise ValueError(f"{field} 必须大于 0")
    return result


def parse_datetime(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
        return datetime.fromtimestamp(timestamp, UTC).astimezone(shanghai).replace(tzinfo=None)
    text = str(value).strip()
    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M%S%f%z",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(text, pattern)
            if parsed.tzinfo is not None:
                shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
                return parsed.astimezone(shanghai).replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    raise ValueError(f"无法识别日期: {text}")


def first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        raise ValueError("平台返回的记录集合不是列表")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError("平台返回的记录集合包含非对象")
    return value
