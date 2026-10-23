"""Replay API. Running replays needs ``replay``; reading results needs ``read``."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.deps import app_state, require_scope
from rootsignal_server.api.errors import ApiError
from rootsignal_server.api.schemas import (
    ReplayReportBody,
    ReplayRunList,
    ReplayRunOut,
    ReplayStart,
    ReplayStarted,
)
from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.replay import service

router = APIRouter(prefix="/v1")
Reader = Annotated[TenantContext, Depends(require_scope("read"))]
Replayer = Annotated[TenantContext, Depends(require_scope("replay"))]


@router.post("/regression-cases/{case_id}/replays", response_model=ReplayStarted,
             status_code=201, summary="Register a replay run (the runner then executes it)")
async def start(request: Request, tenant: Replayer, case_id: uuid.UUID,
                body: ReplayStart) -> ReplayStarted:
    engine = app_state(request).engine

    def run() -> ReplayStarted | None:
        with engine.begin() as conn:
            started = service.register_run(conn, tenant, case_id, purpose=body.purpose,
                                           mode=body.mode, matching=body.match)
        return ReplayStarted.model_validate(started) if started else None

    try:
        started = await run_in_threadpool(run)
    except service.NotReplayable as exc:
        raise ApiError(422, "not_replayable", str(exc)) from exc
    if started is None:
        raise ApiError(404, "not_found", "regression case not found")
    return started


@router.post("/replay-runs/{run_id}/report", response_model=ReplayRunOut,
             summary="Submit what the replay child observed")
async def report(request: Request, tenant: Replayer, run_id: uuid.UUID,
                 body: ReplayReportBody) -> ReplayRunOut:
    engine = app_state(request).engine

    def run() -> ReplayRunOut | None:
        with engine.begin() as conn:
            if not service.submit_report(conn, tenant, run_id, trace_id=body.trace_id,
                                         report=body.report, crashed=body.crashed):
                return None
            return ReplayRunOut.model_validate(service.get_run(conn, tenant, run_id))

    try:
        updated = await run_in_threadpool(run)
    except service.NotReplayable as exc:
        raise ApiError(409, "invalid_report", str(exc)) from exc
    if updated is None:
        raise ApiError(404, "not_found", "replay run not found")
    return updated


@router.get("/replay-runs/{run_id}", response_model=ReplayRunOut, summary="A replay run")
async def get_one(request: Request, tenant: Reader, run_id: uuid.UUID) -> ReplayRunOut:
    engine = app_state(request).engine

    def run() -> ReplayRunOut | None:
        with engine.connect() as conn:
            found = service.get_run(conn, tenant, run_id)
        return ReplayRunOut.model_validate(found) if found else None

    found = await run_in_threadpool(run)
    if found is None:
        raise ApiError(404, "not_found", "replay run not found")
    return found


@router.get("/regression-cases/{case_id}/replays", response_model=ReplayRunList,
            summary="Replay runs of a case, newest first")
async def list_for_case(request: Request, tenant: Reader, case_id: uuid.UUID) -> ReplayRunList:
    engine = app_state(request).engine

    def run() -> ReplayRunList:
        with engine.connect() as conn:
            return ReplayRunList.model_validate(
                {"items": service.list_runs(conn, tenant, case_id)})

    return await run_in_threadpool(run)
