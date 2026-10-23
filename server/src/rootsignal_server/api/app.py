"""FastAPI application factory."""

from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from rootsignal_server import __version__
from rootsignal_server.api import (
    routes_auth,
    routes_ingest,
    routes_investigations,
    routes_issues,
    routes_regression,
    routes_traces,
)
from rootsignal_server.api.deps import AppState, app_state
from rootsignal_server.api.errors import install_error_handlers
from rootsignal_server.auth.rate_limit import RateLimiter
from rootsignal_server.auth.sessions import SessionPolicy
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
        login_limiter=RateLimiter(settings.login_rate_limit_per_minute / 60,
                                  settings.login_rate_limit_per_minute),
        session_policy=SessionPolicy(
            absolute_ttl=timedelta(hours=settings.session_absolute_ttl_hours),
            idle_timeout=timedelta(minutes=settings.session_idle_minutes)),
    )
    install_error_handlers(app)
    app.include_router(routes_ingest.router)
    app.include_router(routes_traces.router)
    app.include_router(routes_issues.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_investigations.router)
    app.include_router(routes_regression.router)

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
