from fastapi import FastAPI

from aftersales_workbench import __version__
from aftersales_workbench.api.router import api_router
from aftersales_workbench.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.app_debug,
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url="/redoc" if settings.app_env != "production" else None,
    )
    application.include_router(api_router)
    return application


app = create_app()
