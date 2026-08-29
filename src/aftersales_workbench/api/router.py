from fastapi import APIRouter

from aftersales_workbench.api.routes import health, warehouse

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(
    warehouse.router,
    prefix="/api/v1/warehouse",
    tags=["仓库人工退货"],
)
