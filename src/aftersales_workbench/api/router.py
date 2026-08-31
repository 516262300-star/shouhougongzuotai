from fastapi import APIRouter

from aftersales_workbench.api.routes import aftersales, health

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(
    aftersales.router,
    prefix="/api/v1/aftersales",
    tags=["售后订单记录"],
)
