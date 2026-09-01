from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.services.aftersales_records import AftersalesRecordService

router = APIRouter()


def get_record_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> AftersalesRecordService:
    return AftersalesRecordService(session)


@router.get("/orders")
def list_orders(
    service: Annotated[AftersalesRecordService, Depends(get_record_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=10, le=100)] = 15,
    record_view: Literal["WORKBENCH", "RECORD_ONLY", "ALL"] = "WORKBENCH",
    shop_id: int | None = None,
    after_sales_type: str | None = None,
    workflow_status: str | None = None,
    logistics_state: str | None = None,
    sales_owner: Annotated[str | None, Query(max_length=50)] = None,
    started_on: date | None = None,
    ended_on: date | None = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    return service.list_orders(
        page=page,
        page_size=page_size,
        record_view=record_view,
        shop_id=shop_id,
        after_sales_type=after_sales_type,
        workflow_status=workflow_status,
        logistics_state=logistics_state,
        sales_owner=sales_owner,
        started_on=started_on,
        ended_on=ended_on,
        keyword=keyword,
    )


@router.get("/intercepts")
def list_intercepts(
    service: Annotated[AftersalesRecordService, Depends(get_record_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=10, le=100)] = 15,
    shop_id: int | None = None,
    sales_owner: Annotated[str | None, Query(max_length=50)] = None,
    stage: Literal[
        "WAITING_NOTICE",
        "NOTICE_SENT",
        "REFUND_BLOCKED",
        "WAITING_RETURN",
        "ERP_MATCH",
        "MANUAL",
    ]
    | None = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    return service.list_intercepts(
        page=page,
        page_size=page_size,
        shop_id=shop_id,
        sales_owner=sales_owner,
        stage=stage,
        keyword=keyword,
    )


@router.get("/orders/{after_sales_sn}")
def get_order(
    after_sales_sn: str,
    service: Annotated[AftersalesRecordService, Depends(get_record_service)],
) -> dict[str, Any]:
    order = service.get_order(after_sales_sn)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="售后订单不存在",
        )
    return order
