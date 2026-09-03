from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.services.runtime_monitor import RuntimeMonitorService

router = APIRouter()


def get_monitor_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> RuntimeMonitorService:
    return RuntimeMonitorService(session)


@router.get("/status")
def runtime_status(
    service: Annotated[RuntimeMonitorService, Depends(get_monitor_service)],
) -> dict[str, Any]:
    return service.get_status()
