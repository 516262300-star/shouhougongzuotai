from fastapi import APIRouter

from aftersales_workbench.api.routes import (
    aftersales,
    attribution,
    health,
    monitor,
    scrap,
    warehouse,
)

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(
    aftersales.router,
    prefix="/api/v1/aftersales",
    tags=["售后订单记录"],
)
api_router.include_router(
    warehouse.router,
    prefix="/api/v1/warehouse",
    tags=["模块 2 仓库验货"],
)
api_router.include_router(
    attribution.router,
    prefix="/api/v1/attribution",
    tags=["模块 4 售后归因"],
)
api_router.include_router(
    scrap.router,
    prefix="/api/v1/scrap",
    tags=["模块 5 退货报废"],
)
api_router.include_router(
    monitor.router,
    prefix="/api/v1/monitor",
    tags=["运行监控"],
)
