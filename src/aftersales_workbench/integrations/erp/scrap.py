from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import ErpReturnRowRecord, ErpScrapSyncState
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchConfigurationError,
    ErpWebReturnMatcher,
    _table_rows,
)

_QUANTITY_HEADERS = ("入库数量", "差值", "数量")


@dataclass(frozen=True, slots=True)
class ErpScrapSourceRow:
    source_row_id: str
    source_status: str | None
    return_order_sn: str
    completed_at: datetime | None
    completed_on: date
    handler: str | None
    product_model: str
    raw_color: str
    normalized_color: str
    is_scrap: bool
    quantity: Decimal
    raw_unit_price: Decimal | None


@dataclass(slots=True)
class ErpScrapSyncResult:
    dry_run: bool
    days_requested: int = 0
    days_synced: int = 0
    rows_seen: int = 0
    scrap_rows_seen: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_deactivated: int = 0
    skipped_recent: bool = False

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decimal(value: str) -> Decimal | None:
    normalized = str(value or "").replace(",", "").strip()
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _parse_datetime(value: str) -> datetime | None:
    normalized = str(value or "").strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    return None


def _normalized_color(raw_color: str) -> str:
    color = raw_color.strip()
    if not color.startswith("报废"):
        return color
    return color[2:].lstrip("+-—_ /：:").strip()


def parse_erp_return_rows(document: str, requested_on: date) -> tuple[ErpScrapSourceRow, ...]:
    """解析 ERP v2 表格，字段名变化时宁可返回空也不误判报废。"""

    rows = _table_rows(document)
    headers: list[str] | None = None
    values_start = 0
    for index, candidate in enumerate(rows):
        header_set = set(candidate)
        if {"型号", "颜色", "编号"}.issubset(header_set) and any(
            name in header_set for name in _QUANTITY_HEADERS
        ):
            headers = candidate
            values_start = index + 1
            break
    if headers is None:
        return ()

    parsed: list[ErpScrapSourceRow] = []
    for values in rows[values_start:]:
        if len(values) != len(headers):
            if parsed:
                break
            continue
        record = dict(zip(headers, values, strict=True))
        return_order_sn = record.get("编号", "").strip()
        model = record.get("型号", "").strip()
        raw_color = record.get("颜色", "").strip()
        if not return_order_sn.startswith("TH-") or not model:
            continue
        quantity = next(
            (
                parsed_quantity
                for name in _QUANTITY_HEADERS
                if (parsed_quantity := _decimal(record.get(name, ""))) is not None
            ),
            None,
        )
        if quantity is None:
            continue
        completed_at = _parse_datetime(record.get("完成日期", ""))
        source_id = (record.get("id") or record.get("ID") or "").strip()
        if not source_id:
            identity = "|".join(
                (
                    return_order_sn,
                    model,
                    raw_color,
                    str(abs(quantity)),
                    str(completed_at or requested_on),
                )
            )
            source_id = f"hash-{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
        parsed.append(
            ErpScrapSourceRow(
                source_row_id=source_id,
                source_status=(record.get("status") or record.get("状态") or "").strip() or None,
                return_order_sn=return_order_sn,
                completed_at=completed_at,
                completed_on=completed_at.date() if completed_at else requested_on,
                handler=(record.get("经办人") or "").strip() or None,
                product_model=model,
                raw_color=raw_color,
                normalized_color=_normalized_color(raw_color),
                is_scrap=raw_color.startswith("报废"),
                quantity=abs(quantity),
                raw_unit_price=_decimal(record.get("单价", "")),
            )
        )
    return tuple(parsed)


class ErpScrapPageClient:
    def __init__(self, matcher: ErpWebReturnMatcher) -> None:
        self.matcher = matcher

    def close(self) -> None:
        self.matcher.close()

    def fetch_day(self, day: date) -> str:
        formatted = day.isoformat()
        return self.matcher._get(
            "/leedis2/public/b4refund/v2",
            params={"start": formatted, "end": formatted, "autouser": ""},
        )


class ErpScrapSyncService:
    STATE_KEY = "erp_return_scrap"

    def __init__(self, session: Session, client: ErpScrapPageClient) -> None:
        self.session = session
        self.client = client

    def sync_days(self, days: Iterable[date], *, dry_run: bool) -> ErpScrapSyncResult:
        unique_days = tuple(dict.fromkeys(days))
        result = ErpScrapSyncResult(dry_run=dry_run, days_requested=len(unique_days))
        for day in unique_days:
            source_rows = parse_erp_return_rows(self.client.fetch_day(day), day)
            result.days_synced += 1
            result.rows_seen += len(source_rows)
            result.scrap_rows_seen += sum(row.is_scrap for row in source_rows)
            created, updated, deactivated = self._apply_day(day, source_rows, dry_run=dry_run)
            result.rows_created += created
            result.rows_updated += updated
            result.rows_deactivated += deactivated
        if not dry_run:
            state = self.session.get(ErpScrapSyncState, self.STATE_KEY)
            if state is None:
                state = ErpScrapSyncState(state_key=self.STATE_KEY)
                self.session.add(state)
            state.last_run_at = datetime.now()
            state.last_successful_on = max(unique_days) if unique_days else None
            state.last_error = None
            self.session.commit()
        return result

    def run_incremental(
        self,
        *,
        refresh_seconds: int,
        lookback_days: int,
        dry_run: bool,
    ) -> ErpScrapSyncResult:
        today = date.today()
        state = self.session.get(ErpScrapSyncState, self.STATE_KEY)
        if (
            state is not None
            and state.last_run_at is not None
            and (datetime.now() - state.last_run_at).total_seconds() < refresh_seconds
        ):
            return ErpScrapSyncResult(dry_run=dry_run, skipped_recent=True)
        oldest = today - timedelta(days=lookback_days - 1)
        reconcile_on = state.next_reconcile_on if state else oldest
        if (
            reconcile_on is None
            or reconcile_on < oldest
            or reconcile_on >= today - timedelta(days=1)
        ):
            reconcile_on = oldest
        days = (today, today - timedelta(days=1), reconcile_on)
        result = self.sync_days(days, dry_run=dry_run)
        if not dry_run:
            state = self.session.get(ErpScrapSyncState, self.STATE_KEY)
            assert state is not None
            candidate = reconcile_on + timedelta(days=1)
            state.next_reconcile_on = candidate if candidate < today - timedelta(days=1) else oldest
            self.session.commit()
        return result

    def _apply_day(
        self,
        day: date,
        source_rows: tuple[ErpScrapSourceRow, ...],
        *,
        dry_run: bool,
    ) -> tuple[int, int, int]:
        source_ids = {row.source_row_id for row in source_rows}
        existing = (
            {
                row.source_row_id: row
                for row in self.session.scalars(
                    select(ErpReturnRowRecord).where(
                        ErpReturnRowRecord.source_row_id.in_(source_ids)
                    )
                )
            }
            if source_ids
            else {}
        )
        created = sum(row.source_row_id not in existing for row in source_rows)
        updated = len(source_rows) - created
        active_for_day = set(
            self.session.scalars(
                select(ErpReturnRowRecord.source_row_id).where(
                    ErpReturnRowRecord.completed_on == day,
                    ErpReturnRowRecord.source_active == 1,
                )
            )
        )
        deactivated = len(active_for_day - source_ids)
        if dry_run:
            return created, updated, deactivated

        now = datetime.now()
        for source in source_rows:
            record = existing.get(source.source_row_id)
            if record is None:
                record = ErpReturnRowRecord(source_row_id=source.source_row_id)
                self.session.add(record)
            record.source_status = source.source_status
            record.return_order_sn = source.return_order_sn
            record.completed_at = source.completed_at
            record.completed_on = source.completed_on
            record.handler = source.handler
            record.product_model = source.product_model
            record.raw_color = source.raw_color
            record.normalized_color = source.normalized_color
            record.is_scrap = int(source.is_scrap)
            record.quantity = source.quantity
            record.raw_unit_price = source.raw_unit_price
            record.source_active = 1
            record.last_seen_at = now
        if active_for_day - source_ids:
            self.session.execute(
                update(ErpReturnRowRecord)
                .where(ErpReturnRowRecord.source_row_id.in_(active_for_day - source_ids))
                .values(source_active=0, last_seen_at=now)
            )
        self.session.flush()
        return created, updated, deactivated


def build_erp_scrap_client(settings: Settings) -> ErpScrapPageClient:
    username = (
        settings.erp_web_username.get_secret_value().strip() if settings.erp_web_username else ""
    )
    password = (
        settings.erp_web_password.get_secret_value().strip() if settings.erp_web_password else ""
    )
    if not settings.erp_web_lookup_enabled or not username or not password:
        raise ErpReturnMatchConfigurationError(
            "ERP 报废同步需要 ERP_WEB_LOOKUP_ENABLED=true 及网页登录凭据"
        )
    matcher = ErpWebReturnMatcher(
        base_url=settings.erp_web_base_url,
        username=username,
        password=password,
        timeout_seconds=settings.erp_web_timeout_seconds,
    )
    return ErpScrapPageClient(matcher)
