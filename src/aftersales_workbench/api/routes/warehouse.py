from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    ItemStatus,
    WarehouseInspectionStatus,
    WarehouseReturnDestination,
    WorkflowStatus,
)
from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.workflows.module2 import (
    ActualReturnItem,
    AssignWarehouseReturnCommand,
    CreateWarehouseReturnCommand,
    InspectWarehouseReturnCommand,
    SqlAlchemyWarehouseReturnRepository,
    WarehouseReturnConflictError,
    WarehouseReturnNotFoundError,
    WarehouseReturnOutcome,
    WarehouseReturnService,
    WarehouseReturnValidationError,
)

router = APIRouter()


class ScanReturnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    return_tracking_number: str = Field(min_length=1, max_length=100)


class ExpectedReturnItemResponse(BaseModel):
    product_code: str
    color: str
    applied_quantity: int


class ReturnCandidateResponse(BaseModel):
    shop_code: str
    shop_name: str
    after_sales_sn: str
    platform_order_sn: str
    workflow_status: WorkflowStatus
    customer_name: str | None
    sales_owner: str | None
    expected_items: list[ExpectedReturnItemResponse]


class ScanReturnResponse(BaseModel):
    return_tracking_number: str
    candidates: list[ReturnCandidateResponse]
    recorded_receipt_sn: str | None


class ActualReturnItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_code: str = Field(min_length=1, max_length=100)
    color: str = Field(default="", max_length=50)
    quantity: int = Field(ge=1, le=1_000_000)
    item_status: ItemStatus = ItemStatus.NORMAL
    remark: str | None = Field(default=None, max_length=255)


class CreateWarehouseReturnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    receipt_sn: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="PDA 或工作台生成的唯一收货单号，用于幂等防重",
    )
    return_tracking_number: str = Field(min_length=1, max_length=100)
    destination: WarehouseReturnDestination = WarehouseReturnDestination.STAGING
    after_sales_sn: str | None = Field(default=None, min_length=1, max_length=100)
    customer_reference: str | None = Field(default=None, min_length=1, max_length=100)
    customer_name: str | None = Field(default=None, min_length=1, max_length=100)
    operator: str = Field(min_length=1, max_length=50)
    carrier_code: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=2_000)
    evidence_urls: list[str] = Field(default_factory=list, max_length=20)
    items: list[ActualReturnItemRequest] = Field(min_length=1, max_length=100)


class AssignCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_reference: str = Field(min_length=1, max_length=100)
    customer_name: str | None = Field(default=None, min_length=1, max_length=100)
    after_sales_sn: str | None = Field(default=None, min_length=1, max_length=100)
    assigned_by: str = Field(min_length=1, max_length=50)


class InspectWarehouseReturnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    result: WarehouseInspectionStatus
    inspected_by: str = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=2_000)
    items: list[ActualReturnItemRequest] = Field(min_length=1, max_length=100)


class ActualReturnItemResponse(BaseModel):
    product_code: str
    color: str
    quantity: int
    item_status: ItemStatus
    remark: str | None


class WarehouseReturnResponse(BaseModel):
    receipt_sn: str
    return_tracking_number: str
    destination: WarehouseReturnDestination
    inspection_status: WarehouseInspectionStatus
    after_sales_sn: str | None
    platform_order_sn: str | None
    customer_reference: str | None
    customer_name: str | None
    operator: str
    assigned_by: str | None
    inspected_by: str | None
    inspection_note: str | None
    created_at: datetime | None
    inspected_at: datetime | None
    items: list[ActualReturnItemResponse]
    duplicate: bool


def get_warehouse_return_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> WarehouseReturnService:
    return WarehouseReturnService(SqlAlchemyWarehouseReturnRepository(session))


@router.post("/scan", response_model=ScanReturnResponse, summary="扫描买家退货运单")
def scan_return(
    payload: ScanReturnRequest,
    service: Annotated[WarehouseReturnService, Depends(get_warehouse_return_service)],
) -> ScanReturnResponse:
    result = service.lookup(payload.return_tracking_number)
    return ScanReturnResponse(
        return_tracking_number=result.return_tracking_number,
        candidates=[
            ReturnCandidateResponse(
                shop_code=candidate.shop_code,
                shop_name=candidate.shop_name,
                after_sales_sn=candidate.after_sales_sn,
                platform_order_sn=candidate.platform_order_sn,
                workflow_status=candidate.workflow_status,
                customer_name=candidate.customer_name,
                sales_owner=candidate.sales_owner,
                expected_items=[
                    ExpectedReturnItemResponse(
                        product_code=item.product_code,
                        color=item.color,
                        applied_quantity=item.applied_quantity,
                    )
                    for item in candidate.expected_items
                ],
            )
            for candidate in result.candidates
        ],
        recorded_receipt_sn=result.recorded_receipt_sn,
    )


@router.get(
    "/returns",
    response_model=list[WarehouseReturnResponse],
    summary="查询仓库退货收货单",
)
def list_returns(
    service: Annotated[WarehouseReturnService, Depends(get_warehouse_return_service)],
    destination: WarehouseReturnDestination | None = None,
    inspection_status: WarehouseInspectionStatus | None = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[WarehouseReturnResponse]:
    return [
        _response(outcome)
        for outcome in service.list_returns(
            destination=destination,
            inspection_status=inspection_status,
            keyword=keyword,
            limit=limit,
        )
    ]


@router.post(
    "/returns",
    response_model=WarehouseReturnResponse,
    status_code=status.HTTP_200_OK,
    summary="登记拆包后的实际收货明细",
)
def create_return(
    payload: CreateWarehouseReturnRequest,
    service: Annotated[WarehouseReturnService, Depends(get_warehouse_return_service)],
) -> WarehouseReturnResponse:
    command = CreateWarehouseReturnCommand(
        receipt_sn=payload.receipt_sn,
        return_tracking_number=payload.return_tracking_number,
        destination=payload.destination,
        after_sales_sn=payload.after_sales_sn,
        customer_reference=payload.customer_reference,
        customer_name=payload.customer_name,
        operator=payload.operator,
        carrier_code=payload.carrier_code,
        note=payload.note,
        evidence_urls=tuple(payload.evidence_urls),
        items=_items(payload.items),
    )
    try:
        return _response(service.create(command))
    except WarehouseReturnConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WarehouseReturnValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.post(
    "/returns/{receipt_sn}/assign-customer",
    response_model=WarehouseReturnResponse,
    summary="把退货暂存单认领到客户档案",
)
def assign_customer(
    receipt_sn: str,
    payload: AssignCustomerRequest,
    service: Annotated[WarehouseReturnService, Depends(get_warehouse_return_service)],
) -> WarehouseReturnResponse:
    try:
        return _response(
            service.assign_customer(
                AssignWarehouseReturnCommand(
                    receipt_sn=receipt_sn,
                    customer_reference=payload.customer_reference,
                    customer_name=payload.customer_name,
                    after_sales_sn=payload.after_sales_sn,
                    assigned_by=payload.assigned_by,
                )
            )
        )
    except WarehouseReturnNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except WarehouseReturnConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/returns/{receipt_sn}/inspection",
    response_model=WarehouseReturnResponse,
    summary="提交仓库验货结论",
)
def inspect_return(
    receipt_sn: str,
    payload: InspectWarehouseReturnRequest,
    service: Annotated[WarehouseReturnService, Depends(get_warehouse_return_service)],
) -> WarehouseReturnResponse:
    try:
        return _response(
            service.inspect(
                InspectWarehouseReturnCommand(
                    receipt_sn=receipt_sn,
                    result=payload.result,
                    inspected_by=payload.inspected_by,
                    note=payload.note,
                    items=_items(payload.items),
                )
            )
        )
    except WarehouseReturnNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except WarehouseReturnConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WarehouseReturnValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def _items(items: list[ActualReturnItemRequest]) -> tuple[ActualReturnItem, ...]:
    return tuple(
        ActualReturnItem(
            product_code=item.product_code,
            color=item.color,
            quantity=item.quantity,
            item_status=item.item_status,
            remark=item.remark,
        )
        for item in items
    )


def _response(outcome: WarehouseReturnOutcome) -> WarehouseReturnResponse:
    return WarehouseReturnResponse(
        receipt_sn=outcome.receipt_sn,
        return_tracking_number=outcome.return_tracking_number,
        destination=outcome.destination,
        inspection_status=outcome.inspection_status,
        after_sales_sn=outcome.after_sales_sn,
        platform_order_sn=outcome.platform_order_sn,
        customer_reference=outcome.customer_reference,
        customer_name=outcome.customer_name,
        operator=outcome.operator,
        assigned_by=outcome.assigned_by,
        inspected_by=outcome.inspected_by,
        inspection_note=outcome.inspection_note,
        created_at=outcome.created_at,
        inspected_at=outcome.inspected_at,
        items=[
            ActualReturnItemResponse(
                product_code=item.product_code,
                color=item.color,
                quantity=item.quantity,
                item_status=item.item_status,
                remark=item.remark,
            )
            for item in outcome.items
        ],
        duplicate=outcome.duplicate,
    )
