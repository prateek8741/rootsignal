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
from rootsignal_server.auth.sessions import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SessionPolicy,
    authenticate_session,
)
from rootsignal_server.config import Settings
from rootsignal_server.ingest.pricing import PriceTable


@dataclass(frozen=True)
class AppState:
    settings: Settings
    engine: Engine
    rate_limiter: RateLimiter
    prices: PriceTable
    login_limiter: RateLimiter
    session_policy: SessionPolicy


def app_state(request: Request) -> AppState:
    state: AppState = request.app.state.rootsignal
    return state


def _authenticate(engine: Engine, token: str) -> TenantContext | None:
    with engine.begin() as conn:
        return authenticate(conn, token)


def _authenticate_session(engine: Engine, token: str,
                          policy: SessionPolicy) -> TenantContext | None:
    with engine.begin() as conn:
        return authenticate_session(conn, token, policy)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_csrf_header(request: Request) -> None:
    """Cross-origin pages can't add custom headers without CORS, which we never serve."""
    if request.headers.get(CSRF_HEADER) != "1":
        raise ApiError(403, "csrf", f"missing {CSRF_HEADER} header")


def require_scope(scope: str | None) -> Callable[[Request], Awaitable[TenantContext]]:
    """Authenticate, then require ``scope`` (``None``: any valid key)."""

    async def dependency(request: Request) -> TenantContext:
        """Bearer API key (SDKs, scripts) or dashboard session cookie (browsers)."""
        state = app_state(request)
        header = request.headers.get("authorization")
        cookie = request.cookies.get(SESSION_COOKIE)
        unauthenticated = ApiError(401, "unauthenticated", "missing, malformed or invalid "
                                   "credentials", {"WWW-Authenticate": "Bearer"})
        tenant: TenantContext | None
        if header is not None:
            token = parse_bearer(header)
            if token is None:
                raise unauthenticated
            tenant = await run_in_threadpool(_authenticate, state.engine, token)
        elif cookie:
            if request.method not in SAFE_METHODS:
                require_csrf_header(request)
            tenant = await run_in_threadpool(_authenticate_session, state.engine, cookie,
                                             state.session_policy)
        else:
            raise unauthenticated
        if tenant is None:
            raise unauthenticated
        if scope is not None and not tenant.allows(scope):
            raise ApiError(403, "forbidden", f"this API key lacks the '{scope}' scope")
        wait_s = state.rate_limiter.try_acquire(tenant.key_id)
        if wait_s > 0:
            raise ApiError(429, "rate_limited", "too many requests for this API key",
                           {"Retry-After": str(max(math.ceil(wait_s), 1))})
        return tenant

    return dependency
