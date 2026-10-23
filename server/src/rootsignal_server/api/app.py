"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from rootsignal_server import __version__
from rootsignal_server.api import routes_ingest, routes_traces
from rootsignal_server.api.deps import AppState, app_state
from rootsignal_server.api.errors import install_error_handlers
from rootsignal_server.auth.rate_limit import RateLimiter
from rootsignal_server.config import Settings, get_settings
from rootsignal_server.ingest.pricing import PriceTable
from rootsignal_server.storage.db import make_engine


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Rootsignal API", version=__version__)
    app.state.rootsignal = AppState(
        settings=settings,
        engine=engine or make_engine(settings),
        rate_limiter=RateLimiter(settings.rate_limit_per_second, settings.rate_limit_burst),
        prices=PriceTable.load(settings.pricing_file),
    )
    install_error_handlers(app)
    app.include_router(routes_ingest.router)
    app.include_router(routes_traces.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        engine = app_state(request).engine

        def ping() -> None:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))

        try:
            await run_in_threadpool(ping)
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app
