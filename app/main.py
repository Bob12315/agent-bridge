from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.adapters.registry import build_adapter_registry
from app.bridge.request_manager import RequestManager
from app.bridge.router import Router
from app.config import AppConfig
from app.storage.database import Database
from app.web.routes import router as web_router


def version() -> str:
    return __version__


def create_app(
    config: AppConfig | None = None,
    *,
    database: Database | None = None,
    request_manager: RequestManager | None = None,
) -> FastAPI:
    settings = config or AppConfig()
    storage = database or Database(settings.runtime.database)
    if request_manager is None:
        bridge_router = Router(build_adapter_registry(settings), storage)
        request_manager = RequestManager(storage, bridge_router)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await storage.initialize()
        yield

    application = FastAPI(
        title="Agent Bridge",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.config = settings
    application.state.database = storage
    application.state.request_manager = request_manager
    if settings.web.enabled:
        static = Path(__file__).parent / "web" / "static"
        application.mount("/static", StaticFiles(directory=static), name="static")
        application.include_router(web_router)
    return application


app = create_app()
