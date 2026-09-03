from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.db.models import (
    ErpReturnRowRecord,
    ErpReturnScrapDecision,
    ErpScrapSyncState,
)


def _number(value: Decimal | int | None, places: int = 4) -> float:
    return round(float(value or 0), places)


def _status(row: ErpReturnRowRecord) -> str:
    decision = row.scrap_decision
    if decision is None or not (decision.scrap_reason or "").strip():
        return "MISSING_REASON"
    if decision.loss_amount is None or not (decision.reviewer or "").strip():
        return "MISSING_COST"
    return "CONFIRMED"


class ScrapAnalyticsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def overview(
        self,
        *,
        started_on: date | None,
        ended_on: date | None,
        model_keyword: str | None,
        reason: str | None,
        responsibility: str | None,
        data_status: str | None,
        focus_model: str | None,
    ) -> dict[str, Any]:
        end = ended_on or date.today()
        start = started_on or end - timedelta(days=29)
        rows = list(
            self.session.scalars(
                select(ErpReturnRowRecord)
                .options(selectinload(ErpReturnRowRecord.scrap_decision))
                .where(
                    ErpReturnRowRecord.source_active == 1,
                    ErpReturnRowRecord.completed_on.between(start, end),
                )
                .order_by(ErpReturnRowRecord.completed_on, ErpReturnRowRecord.id)
            )
        )
        keyword = (model_keyword or "").strip().lower()
        denominator_rows = [
            row for row in rows if not keyword or keyword in row.product_model.lower()
        ]
        scrap_rows = [row for row in denominator_rows if row.is_scrap]
        if reason:
            scrap_rows = [
                row
                for row in scrap_rows
                if (row.scrap_decision.scrap_reason if row.scrap_decision else None) == reason
            ]
        if responsibility:
            scrap_rows = [
                row
                for row in scrap_rows
                if (row.scrap_decision.responsibility if row.scrap_decision else None)
                == responsibility
            ]
        if data_status:
            scrap_rows = [row for row in scrap_rows if _status(row) == data_status]

        total_return = sum((row.quantity for row in denominator_rows), Decimal())
        total_scrap = sum((row.quantity for row in scrap_rows), Decimal())
        confirmed_loss = sum(
            (
                row.scrap_decision.loss_amount
                for row in scrap_rows
                if _status(row) == "CONFIRMED" and row.scrap_decision
            ),
            Decimal(),
        )
        by_model_returns: dict[str, Decimal] = defaultdict(Decimal)
        for row in denominator_rows:
            by_model_returns[row.product_model] += row.quantity
        by_model_scrap: dict[str, list[ErpReturnRowRecord]] = defaultdict(list)
        for row in scrap_rows:
            by_model_scrap[row.product_model].append(row)

        models = []
        model_names = (
            by_model_scrap.keys()
            if reason or responsibility or data_status
            else by_model_returns.keys()
        )
        for model in model_names:
            model_scrap_rows = by_model_scrap.get(model, [])
            scrap_quantity = sum((row.quantity for row in model_scrap_rows), Decimal())
            loss = sum(
                (
                    row.scrap_decision.loss_amount
                    for row in model_scrap_rows
                    if _status(row) == "CONFIRMED" and row.scrap_decision
                ),
                Decimal(),
            )
            status_counts: dict[str, int] = defaultdict(int)
            responsibilities: dict[str, Decimal] = defaultdict(Decimal)
            for row in model_scrap_rows:
                status_counts[_status(row)] += 1
                if row.scrap_decision and row.scrap_decision.responsibility:
                    responsibilities[row.scrap_decision.responsibility] += row.quantity
            model_return = by_model_returns[model]
            models.append(
                {
                    "model": model,
                    "return_quantity": _number(model_return),
                    "scrap_quantity": _number(scrap_quantity),
                    "scrap_rate": _number(
                        scrap_quantity / model_return * 100 if model_return else 0
                    ),
                    "confirmed_loss": _number(loss, 2),
                    "loss_share": _number(loss / confirmed_loss * 100 if confirmed_loss else 0),
                    "responsibility": max(responsibilities, key=responsibilities.get)
                    if responsibilities
                    else "待确认",
                    "data_status": (
                        max(status_counts, key=status_counts.get)
                        if status_counts
                        else "NO_SCRAP"
                    ),
                    "status_counts": dict(status_counts),
                }
            )
        models.sort(key=lambda item: (-item["scrap_quantity"], -item["scrap_rate"], item["model"]))

        reasons = self._distribution(scrap_rows, "reason")
        trends = []
        current = start
        while current <= end:
            day_rows = [row for row in denominator_rows if row.completed_on == current]
            day_scrap = [row for row in scrap_rows if row.completed_on == current]
            day_return_quantity = sum((row.quantity for row in day_rows), Decimal())
            day_scrap_quantity = sum((row.quantity for row in day_scrap), Decimal())
            trends.append(
                {
                    "date": current.isoformat(),
                    "rate": _number(
                        day_scrap_quantity / day_return_quantity * 100 if day_return_quantity else 0
                    ),
                    "return_quantity": _number(day_return_quantity),
                    "scrap_quantity": _number(day_scrap_quantity),
                }
            )
            current += timedelta(days=1)

        chosen_model = focus_model or (models[0]["model"] if models else None)
        focus_rows = [row for row in scrap_rows if row.product_model == chosen_model]
        state = self.session.get(ErpScrapSyncState, "erp_return_scrap")
        return {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "summary": {
                "return_quantity": _number(total_return),
                "scrap_quantity": _number(total_scrap),
                "scrap_rate": _number(total_scrap / total_return * 100 if total_return else 0),
                "confirmed_loss": _number(confirmed_loss, 2),
                "scrap_orders": len({row.return_order_sn for row in scrap_rows}),
                "scrap_rows": len(scrap_rows),
            },
            "models": models,
            "reasons": reasons,
            "trend": trends,
            "focus": self._focus(
                chosen_model,
                focus_rows,
                by_model_returns.get(chosen_model or "", Decimal()),
                confirmed_loss,
            ),
            "options": {
                "reasons": sorted(
                    {
                        row.scrap_decision.scrap_reason
                        for row in rows
                        if row.scrap_decision and row.scrap_decision.scrap_reason
                    }
                ),
                "responsibilities": sorted(
                    {
                        row.scrap_decision.responsibility
                        for row in rows
                        if row.scrap_decision and row.scrap_decision.responsibility
                    }
                ),
            },
            "sync": {
                "last_run_at": state.last_run_at.isoformat()
                if state and state.last_run_at
                else None,
                "last_successful_on": state.last_successful_on.isoformat()
                if state and state.last_successful_on
                else None,
            },
        }

    @staticmethod
    def _distribution(rows: list[ErpReturnRowRecord], dimension: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {"quantity": Decimal(), "loss": Decimal()}
        )
        total = sum((row.quantity for row in rows), Decimal())
        for row in rows:
            decision = row.scrap_decision
            if dimension == "color":
                key = row.normalized_color or "未标颜色"
            else:
                key = decision.scrap_reason if decision and decision.scrap_reason else "待补原因"
            grouped[key]["quantity"] += row.quantity
            if decision and _status(row) == "CONFIRMED":
                grouped[key]["loss"] += decision.loss_amount or Decimal()
        output = [
            {
                "name": name,
                "value": _number(values["quantity"]),
                "share": _number(values["quantity"] / total * 100 if total else 0),
                "loss": _number(values["loss"], 2),
            }
            for name, values in grouped.items()
        ]
        return sorted(output, key=lambda item: (-item["value"], item["name"]))

    def _focus(
        self,
        model: str | None,
        rows: list[ErpReturnRowRecord],
        return_quantity: Decimal,
        company_loss: Decimal,
    ) -> dict[str, Any] | None:
        if model is None:
            return None
        scrap_quantity = sum((row.quantity for row in rows), Decimal())
        loss = sum(
            (
                row.scrap_decision.loss_amount
                for row in rows
                if row.scrap_decision and _status(row) == "CONFIRMED"
            ),
            Decimal(),
        )
        records = []
        for row in sorted(rows, key=lambda item: (item.completed_on, item.id), reverse=True):
            decision = row.scrap_decision
            records.append(
                {
                    "source_row_id": row.source_row_id,
                    "return_order_sn": row.return_order_sn,
                    "completed_on": row.completed_on.isoformat(),
                    "model": row.product_model,
                    "raw_color": row.raw_color,
                    "color": row.normalized_color,
                    "quantity": _number(row.quantity),
                    "reason": decision.scrap_reason if decision else None,
                    "responsibility": decision.responsibility if decision else None,
                    "confirmed_unit_cost": _number(decision.confirmed_unit_cost, 4)
                    if decision and decision.confirmed_unit_cost is not None
                    else None,
                    "loss_amount": _number(decision.loss_amount, 2)
                    if decision and decision.loss_amount is not None
                    else None,
                    "cost_source": decision.cost_source if decision else None,
                    "reviewer": decision.reviewer if decision else None,
                    "data_status": _status(row),
                }
            )
        return {
            "model": model,
            "return_quantity": _number(return_quantity),
            "scrap_quantity": _number(scrap_quantity),
            "scrap_rate": _number(scrap_quantity / return_quantity * 100 if return_quantity else 0),
            "confirmed_loss": _number(loss, 2),
            "loss_share": _number(loss / company_loss * 100 if company_loss else 0),
            "colors": self._distribution(rows, "color"),
            "reasons": self._distribution(rows, "reason"),
            "records": records,
        }

    def save_decision(
        self,
        source_row_id: str,
        *,
        scrap_reason: str | None,
        responsibility: str | None,
        confirmed_unit_cost: Decimal | None,
        loss_amount: Decimal | None,
        cost_source: str | None,
        reviewer: str | None,
        evidence_urls: list[str] | None,
    ) -> dict[str, Any] | None:
        row = self.session.scalar(
            select(ErpReturnRowRecord)
            .options(selectinload(ErpReturnRowRecord.scrap_decision))
            .where(ErpReturnRowRecord.source_row_id == source_row_id)
        )
        if row is None or not row.is_scrap:
            return None
        decision = row.scrap_decision
        if decision is None:
            decision = ErpReturnScrapDecision(erp_return_row=row)
            self.session.add(decision)
        decision.scrap_reason = (scrap_reason or "").strip() or None
        decision.responsibility = (responsibility or "").strip() or None
        decision.confirmed_unit_cost = confirmed_unit_cost
        if loss_amount is None and confirmed_unit_cost is not None:
            loss_amount = (row.quantity * confirmed_unit_cost).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        decision.loss_amount = loss_amount
        decision.cost_source = (cost_source or "").strip() or None
        decision.reviewer = (reviewer or "").strip() or None
        decision.evidence_urls = evidence_urls
        decision.confirmed_at = datetime.now() if _status(row) == "CONFIRMED" else None
        self.session.commit()
        self.session.refresh(decision)
        return {"source_row_id": source_row_id, "data_status": _status(row)}
