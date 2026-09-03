from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.services.scrap_analytics import ScrapAnalyticsService

router = APIRouter()


class ScrapDecisionInput(BaseModel):
    scrap_reason: str | None = Field(default=None, max_length=100)
    responsibility: str | None = Field(default=None, max_length=50)
    confirmed_unit_cost: Decimal | None = Field(default=None, ge=0)
    loss_amount: Decimal | None = Field(default=None, ge=0)
    cost_source: str | None = Field(default=None, max_length=100)
    reviewer: str | None = Field(default=None, max_length=50)
    evidence_urls: list[str] | None = None


def get_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> ScrapAnalyticsService:
    return ScrapAnalyticsService(session)


@router.get("/overview")
def overview(
    service: Annotated[ScrapAnalyticsService, Depends(get_service)],
    started_on: date | None = None,
    ended_on: date | None = None,
    model_keyword: Annotated[str | None, Query(max_length=100)] = None,
    reason: Annotated[str | None, Query(max_length=100)] = None,
    responsibility: Annotated[str | None, Query(max_length=50)] = None,
    data_status: Literal["MISSING_REASON", "MISSING_COST", "CONFIRMED"] | None = None,
    focus_model: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    if started_on and ended_on and ended_on < started_on:
        raise HTTPException(status_code=422, detail="结束日期不能早于开始日期")
    return service.overview(
        started_on=started_on,
        ended_on=ended_on,
        model_keyword=model_keyword,
        reason=reason,
        responsibility=responsibility,
        data_status=data_status,
        focus_model=focus_model,
    )


@router.patch("/records/{source_row_id}/decision")
def save_decision(
    source_row_id: str,
    payload: ScrapDecisionInput,
    service: Annotated[ScrapAnalyticsService, Depends(get_service)],
) -> dict[str, Any]:
    result = service.save_decision(source_row_id, **payload.model_dump())
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该 ERP 报废记录")
    return result
