from fastapi import APIRouter

from aftersales_workbench.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
