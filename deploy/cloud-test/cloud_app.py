"""Read-only cloud trial entrypoint; never starts the local automation worker."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from aftersales_workbench.api.router import api_router


def create_cloud_app(frontend_dir: Path | None = None) -> FastAPI:
    frontend_dir = frontend_dir or Path(__file__).resolve().parents[2] / "frontend/dist/client"
    if not (frontend_dir / "index.html").is_file():
        raise RuntimeError("Frontend build missing: build the cloud-test bundle first")
    application = FastAPI(
        title="利德仕售后工作台 · 云端只读测试",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def read_only_trial(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse(
                status_code=403,
                content={"detail": "云端只读测试版暂不开放修改、退款或自动发布操作"},
            )
        return await call_next(request)

    application.include_router(api_router)
    application.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    return application


app = create_cloud_app()
