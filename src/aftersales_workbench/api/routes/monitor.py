from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.services.desktop_notice_recovery import (
    DesktopNoticeRecoveryService,
)
from aftersales_workbench.services.integration_capabilities import (
    IntegrationCapabilityService,
)
from aftersales_workbench.services.runtime_issues import RuntimeIssueCollector, RuntimeIssueService
from aftersales_workbench.services.runtime_monitor import RuntimeMonitorService
from aftersales_workbench.workflows.desktop_sender import DesktopNoticeSendError

router = APIRouter()


def get_issue_service(session: Annotated[Session, Depends(get_db_session)]) -> RuntimeIssueService:
    root = get_runtime_root()
    return RuntimeIssueService(
        RuntimeIssueCollector(session, get_settings(), root),
        root / ".runtime" / "monitor-incidents.sqlite3",
    )


@router.get("/issues")
def runtime_issues(
    service: Annotated[RuntimeIssueService, Depends(get_issue_service)],
    state: Literal["OPEN", "RESOLVED", "STOPPED", "ALL"] = "OPEN",
    category: Literal["ERP", "NOTICE", "REFUND", "LOGISTICS", "SYNC", "TODO", "OTHER"]
    | None = None,
    platform: Literal["PDD", "TMALL", "TAOBAO", "1688", "JD", "DOUYIN"] | None = None,
    shop_id: Annotated[int | None, Query(gt=0)] = None,
    keyword: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 15,
) -> dict[str, Any]:
    try:
        return service.list_issues(
            state=state,
            category=category,
            platform=platform,
            shop_id=shop_id,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        # 不向浏览器回显数据库连接串、凭据或原始 API 响应。
        raise HTTPException(
            status_code=503,
            detail="异常明细暂时无法完整读取，保留原记录；请稍后刷新或联系维护人员。",
        ) from exc


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
