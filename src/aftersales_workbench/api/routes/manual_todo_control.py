from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import get_db_session
from aftersales_workbench.services.manual_todo_control import (
    ManualTodoControlService,
    PublishControlConflict,
)

router = APIRouter()


class PublishControlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    expected_version: Annotated[StrictInt, Field(ge=0)]


def get_control_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> ManualTodoControlService:
    return ManualTodoControlService(session, get_settings())


def require_local_control_request(request: Request) -> None:
    # 现有工作台无账号权限系统；仅允许本机同源 UI 修改，不把外部发送开关暴露到局域网。
    loopback = {"127.0.0.1", "::1", "localhost"}
    if not request.client or request.client.host not in loopback:
        raise HTTPException(403, "请在运行售后工作台的本机操作发布开关")
    if request.url.hostname not in loopback:
        raise HTTPException(403, "发布开关仅允许本机地址访问")
    if request.headers.get("X-Workbench-Action") != "manual-todo-publish-switch":
        raise HTTPException(403, "缺少发布开关操作标识")
    try:
        origin = urlsplit(request.headers.get("origin", ""))
    except ValueError as exc:
        raise HTTPException(403, "无效的页面来源") from exc
    if (
        origin.scheme != request.url.scheme or origin.netloc != request.url.netloc
        or origin.path or origin.query or origin.fragment
    ):
        raise HTTPException(403, "发布开关只接受本机同源页面操作")


@router.get("/manual-todos/publishing")
def get_publishing(
    service: Annotated[ManualTodoControlService, Depends(get_control_service)],
) -> dict:
    try:
        return service.get_status()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "人工待办开关暂时不可读取，请检查数据库迁移及服务") from exc


@router.put("/manual-todos/publishing", dependencies=[Depends(require_local_control_request)])
def set_publishing(
    update: PublishControlUpdate,
    service: Annotated[ManualTodoControlService, Depends(get_control_service)],
) -> dict:
    try:
        return service.set_enabled(
            enabled=update.enabled, expected_version=update.expected_version,
        )
    except PublishControlConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "发布开关保存结果未确认，请先刷新核实，勿连续重试") from exc
