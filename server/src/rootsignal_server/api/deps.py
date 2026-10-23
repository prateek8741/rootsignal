"""Request dependencies: authentication, scope checks, rate limiting."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Engine
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.errors import ApiError
from rootsignal_server.auth.keys import TenantContext, authenticate, parse_bearer
from rootsignal_server.auth.rate_limit import RateLimiter
from rootsignal_server.config import Settings
from rootsignal_server.ingest.pricing import PriceTable


@dataclass(frozen=True)
class AppState:
    settings: Settings
    engine: Engine
    rate_limiter: RateLimiter
    prices: PriceTable


def app_state(request: Request) -> AppState:
    state: AppState = request.app.state.rootsignal
    return state


def _authenticate(engine: Engine, token: str) -> TenantContext | None:
    with engine.begin() as conn:
        return authenticate(conn, token)


def require_scope(scope: str) -> Callable[[Request], Awaitable[TenantContext]]:
    async def dependency(request: Request) -> TenantContext:
        token = parse_bearer(request.headers.get("authorization"))
        unauthenticated = ApiError(401, "unauthenticated", "missing, malformed or invalid API key",
                                   {"WWW-Authenticate": "Bearer"})
        if token is None:
            raise unauthenticated
        state = app_state(request)
        tenant = await run_in_threadpool(_authenticate, state.engine, token)
        if tenant is None:
            raise unauthenticated
        if not tenant.allows(scope):
            raise ApiError(403, "forbidden", f"this API key lacks the '{scope}' scope")
        wait_s = state.rate_limiter.try_acquire(tenant.key_id)
        if wait_s > 0:
            raise ApiError(429, "rate_limited", "too many requests for this API key",
                           {"Retry-After": str(max(math.ceil(wait_s), 1))})
        return tenant

    return dependency
