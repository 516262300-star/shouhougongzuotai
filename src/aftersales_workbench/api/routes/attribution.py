from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.services.refund_attribution import RefundAttributionService

router = APIRouter()


def get_attribution_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> RefundAttributionService:
    return RefundAttributionService(session)


@router.get("/overview")
def overview(
    service: Annotated[RefundAttributionService, Depends(get_attribution_service)],
    shop_id: int | None = None,
    started_on: date | None = None,
    ended_on: date | None = None,
    model_keyword: Annotated[str | None, Query(max_length=100)] = None,
    reason_category: Literal[
        "DISLIKE",
        "QUALITY",
        "SPEC_MISMATCH",
        "LOGISTICS",
        "DESCRIPTION",
        "PRICE",
        "OTHER",
    ]
    | None = None,
    focus_model: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    return service.overview(
        shop_id=shop_id,
        started_on=started_on,
        ended_on=ended_on,
        model_keyword=model_keyword,
        reason_category=reason_category,
        focus_model=focus_model,
    )
