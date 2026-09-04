from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.services.desktop_notice_recovery import (
    DesktopNoticeRecoveryService,
)
from aftersales_workbench.services.integration_capabilities import (
    IntegrationCapabilityService,
)
from aftersales_workbench.services.runtime_monitor import RuntimeMonitorService
from aftersales_workbench.workflows.desktop_sender import DesktopNoticeSendError

router = APIRouter()


def get_monitor_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> RuntimeMonitorService:
    return RuntimeMonitorService(session)


def get_desktop_recovery_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> DesktopNoticeRecoveryService:
    return DesktopNoticeRecoveryService(session)


def get_capability_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> IntegrationCapabilityService:
    return IntegrationCapabilityService(session)


@router.get("/status")
def runtime_status(
    service: Annotated[RuntimeMonitorService, Depends(get_monitor_service)],
) -> dict[str, Any]:
    return service.get_status()


@router.get("/capabilities")
def integration_capabilities(
    service: Annotated[IntegrationCapabilityService, Depends(get_capability_service)],
) -> dict[str, Any]:
    return service.get_capabilities()


@router.post("/desktop-notifications/{task_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_desktop_notification(
    task_id: int,
    service: Annotated[
        DesktopNoticeRecoveryService,
        Depends(get_desktop_recovery_service),
    ],
) -> dict[str, Any]:
    try:
        return service.retry_before_paste(task_id).safe_dict()
    except DesktopNoticeSendError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
