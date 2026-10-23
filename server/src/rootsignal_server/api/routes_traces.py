"""Read API: trace list, trace detail, payload bodies. Requires the ``read`` scope."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.deps import app_state, require_scope
from rootsignal_server.api.errors import ApiError
from rootsignal_server.api.schemas import PayloadOut, TraceDetail, TraceList
from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.storage import queries

router = APIRouter(prefix="/v1")
Reader = Annotated[TenantContext, Depends(require_scope("read"))]
TraceId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Sha256 = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


@router.get("/traces", response_model=TraceList, summary="List traces, newest first")
async def list_traces(
    request: Request,
    tenant: Reader,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    status: Annotated[str | None, Query(pattern="^(unset|ok|error)$")] = None,
    agent_name: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> TraceList:
    engine = app_state(request).engine
    filters = queries.TraceFilters(status=status, agent_name=agent_name, since=since,
                                   until=until)

    def run() -> TraceList:
        with engine.connect() as conn:
            items, next_cursor = queries.list_traces(conn, tenant, filters, limit=limit,
                                                     cursor=cursor)
        return TraceList.model_validate({"items": items, "next_cursor": next_cursor})

    try:
        return await run_in_threadpool(run)
    except queries.InvalidCursor as exc:
        raise ApiError(400, "invalid_cursor", str(exc)) from exc


@router.get("/traces/{trace_id}", response_model=TraceDetail, summary="Trace with its spans")
async def get_trace(request: Request, tenant: Reader, trace_id: TraceId) -> TraceDetail:
    engine = app_state(request).engine

    def run() -> TraceDetail | None:
        with engine.connect() as conn:
            found = queries.get_trace(conn, tenant, trace_id)
        if found is None:
            return None
        trace, spans = found
        return TraceDetail.model_validate({"trace": trace, "spans": spans})

    detail = await run_in_threadpool(run)
    if detail is None:
        raise ApiError(404, "not_found", "trace not found")
    return detail


@router.get("/payloads/{sha256}", response_model=PayloadOut, summary="Payload body")
async def get_payload(request: Request, tenant: Reader, sha256: Sha256) -> PayloadOut:
    engine = app_state(request).engine

    def run() -> PayloadOut | None:
        with engine.connect() as conn:
            row = queries.get_payload(conn, tenant, sha256)
        return PayloadOut.model_validate(row) if row else None

    payload = await run_in_threadpool(run)
    if payload is None:
        raise ApiError(404, "not_found", "payload not found")
    return payload
