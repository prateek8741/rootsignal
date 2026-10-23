"""Dashboard sign-in: one-time login links or a pasted key → httpOnly session cookie.

All four endpoints require the ``X-Rootsignal-CSRF: 1`` header for unsafe
methods, including the unauthenticated login calls: otherwise a hostile site
could sign a victim's browser into the attacker's session (login CSRF).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.deps import AppState, app_state, require_csrf_header, require_scope
from rootsignal_server.api.errors import ApiError
from rootsignal_server.api.schemas import Context
from rootsignal_server.auth.keys import TenantContext, authenticate, parse_bearer
from rootsignal_server.auth.sessions import (
    SESSION_COOKIE,
    NewSession,
    authenticate_session,
    redeem_login_link,
    revoke_session,
    session_from_key,
)
from rootsignal_server.storage import queries

router = APIRouter(prefix="/v1/auth")
AnyCredential = Annotated[TenantContext, Depends(require_scope(None))]


class KeyLogin(BaseModel):
    api_key: str = Field(min_length=10, max_length=128)


class LinkLogin(BaseModel):
    token: str = Field(min_length=20, max_length=128)


def _limit_login(request: Request, state: AppState) -> None:
    client = request.client.host if request.client else "unknown"
    if state.login_limiter.try_acquire(("login", client)) > 0:
        raise ApiError(429, "rate_limited", "too many sign-in attempts; wait a minute",
                       {"Retry-After": "60"})


def _set_cookie(response: Response, session: NewSession, state: AppState) -> None:
    max_age = int((session.expires_at - datetime.now(UTC)).total_seconds())
    response.set_cookie(SESSION_COOKIE, session.token, max_age=max_age, path="/v1",
                        httponly=True, samesite="strict",
                        secure=state.settings.session_cookie_secure)


def _context(state: AppState, tenant: TenantContext) -> Context:
    configured = state.settings.llm_provider != "none"
    with state.engine.connect() as conn:
        data = queries.get_context(conn, tenant, llm_configured=configured)
    return Context.model_validate(data)


def _session_context(state: AppState, session: NewSession) -> Context:
    """Describe a just-created session exactly as later requests will see it."""
    with state.engine.begin() as conn:
        tenant = authenticate_session(conn, session.token, state.session_policy)
    if tenant is None:  # revoked or expired between creation and now
        raise ApiError(401, "unauthenticated", "session ended")
    return _context(state, tenant)


@router.post("/session", response_model=Context, summary="Sign in by exchanging an API key")
async def login_with_key(request: Request, response: Response, body: KeyLogin) -> Context:
    require_csrf_header(request)
    state = app_state(request)
    _limit_login(request, state)
    token = parse_bearer(f"Bearer {body.api_key.strip()}")
    if token is None:
        raise ApiError(401, "unauthenticated", "that key wasn't accepted")

    def run() -> NewSession | None:
        with state.engine.begin() as conn:
            key = authenticate(conn, token)
            if key is None:
                return None
            session = session_from_key(conn, key, state.session_policy)
            if session is None:
                raise ApiError(403, "forbidden", "the key needs the 'read' scope")
            return session

    session = await run_in_threadpool(run)
    if session is None:
        raise ApiError(401, "unauthenticated", "that key wasn't accepted")
    _set_cookie(response, session, state)
    return await run_in_threadpool(_session_context, state, session)


@router.post("/link", response_model=Context, summary="Sign in with a one-time login link")
async def login_with_link(request: Request, response: Response, body: LinkLogin) -> Context:
    require_csrf_header(request)
    state = app_state(request)
    _limit_login(request, state)

    def run() -> NewSession | None:
        with state.engine.begin() as conn:
            return redeem_login_link(conn, body.token, state.session_policy)

    session = await run_in_threadpool(run)
    if session is None:
        raise ApiError(401, "invalid_link", "this login link is invalid, expired or already used")
    _set_cookie(response, session, state)
    return await run_in_threadpool(_session_context, state, session)


@router.get("/session", response_model=Context, summary="Who is signed in")
async def current_session(request: Request, tenant: AnyCredential) -> Context:
    return await run_in_threadpool(_context, app_state(request), tenant)


@router.delete("/session", status_code=204, summary="Sign out (ends the session server-side)")
async def logout(request: Request) -> Response:
    require_csrf_header(request)
    state = app_state(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        def run() -> None:
            with state.engine.begin() as conn:
                revoke_session(conn, token)

        await run_in_threadpool(run)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/v1", httponly=True, samesite="strict",
                           secure=state.settings.session_cookie_secure)
    return response
