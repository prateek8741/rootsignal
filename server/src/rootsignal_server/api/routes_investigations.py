"""Investigation API. Starting one needs ``triage`` (it creates work and may spend
LLM tokens); reading needs ``read``."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.deps import app_state, require_scope
from rootsignal_server.api.errors import ApiError
from rootsignal_server.api.schemas import (
    InvestigationDetail,
    InvestigationList,
    InvestigationRequest,
)
from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.investigation import service

router = APIRouter(prefix="/v1")
Reader = Annotated[TenantContext, Depends(require_scope("read"))]
Triager = Annotated[TenantContext, Depends(require_scope("triage"))]


@router.post("/issues/{issue_id}/investigations", response_model=InvestigationDetail,
             status_code=202, summary="Start an investigation (runs in the worker)")
async def start(request: Request, tenant: Triager, issue_id: uuid.UUID,
                body: InvestigationRequest) -> InvestigationDetail:
    engine = app_state(request).engine

    def run() -> InvestigationDetail | None:
        with engine.begin() as conn:
            investigation_id = service.request_investigation(conn, tenant, issue_id,
                                                             use_llm=body.use_llm)
            if investigation_id is None:
                return None
            found = service.get_investigation(conn, tenant, investigation_id)
        return InvestigationDetail.model_validate(found)

    created = await run_in_threadpool(run)
    if created is None:
        raise ApiError(404, "not_found", "issue not found")
    return created


@router.get("/issues/{issue_id}/investigations", response_model=InvestigationList,
            summary="Recent investigations of an issue")
async def list_for_issue(request: Request, tenant: Reader,
                         issue_id: uuid.UUID) -> InvestigationList:
    engine = app_state(request).engine

    def run() -> InvestigationList | None:
        with engine.connect() as conn:
            items = service.list_investigations(conn, tenant, issue_id)
        return None if items is None else InvestigationList.model_validate({"items": items})

    found = await run_in_threadpool(run)
    if found is None:
        raise ApiError(404, "not_found", "issue not found")
    return found


@router.get("/investigations/{investigation_id}", response_model=InvestigationDetail,
            summary="An investigation with its claims")
async def get_one(request: Request, tenant: Reader,
                  investigation_id: uuid.UUID) -> InvestigationDetail:
    engine = app_state(request).engine

    def run() -> InvestigationDetail | None:
        with engine.connect() as conn:
            found = service.get_investigation(conn, tenant, investigation_id)
        return InvestigationDetail.model_validate(found) if found else None

    found = await run_in_threadpool(run)
    if found is None:
        raise ApiError(404, "not_found", "investigation not found")
    return found
