"""Issue API. Reads need ``read``; status changes need ``triage``."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.deps import app_state, require_scope
from rootsignal_server.api.errors import ApiError
from rootsignal_server.api.schemas import (
    IssueDetail,
    IssueList,
    IssueSummary,
    OccurrenceList,
    StatusChange,
)
from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.issues import queries
from rootsignal_server.issues.service import InvalidTransition, change_status

router = APIRouter(prefix="/v1/issues")
Reader = Annotated[TenantContext, Depends(require_scope("read"))]
Triager = Annotated[TenantContext, Depends(require_scope("triage"))]
NOT_FOUND = ApiError(404, "not_found", "issue not found")


@router.get("", response_model=IssueList, summary="List issues")
async def list_issues(
    request: Request,
    tenant: Reader,
    status: Annotated[str | None, Query(pattern="^(open|resolved|ignored)$")] = None,
    severity: Annotated[str | None, Query(pattern="^(critical|high|medium|low)$")] = None,
    detector: str | None = None,
    agent_name: str | None = None,
    regression: bool | None = None,
    sort: Literal["last_seen", "occurrences", "severity"] = "last_seen",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> IssueList:
    engine = app_state(request).engine
    filters = queries.IssueFilters(status, severity, detector, agent_name, regression)

    def run() -> IssueList:
        with engine.connect() as conn:
            items, next_cursor = queries.list_issues(conn, tenant, filters, sort=sort,
                                                     limit=limit, cursor=cursor)
        return IssueList.model_validate({"items": items, "next_cursor": next_cursor})

    try:
        return await run_in_threadpool(run)
    except queries.InvalidCursor as exc:
        raise ApiError(400, "invalid_cursor", str(exc)) from exc


@router.get("/{issue_id}", response_model=IssueDetail,
            summary="Issue with versions, history and recent occurrences")
async def get_issue(request: Request, tenant: Reader, issue_id: uuid.UUID) -> IssueDetail:
    engine = app_state(request).engine

    def run() -> IssueDetail | None:
        with engine.connect() as conn:
            found = queries.get_issue(conn, tenant, issue_id)
        return IssueDetail.model_validate(found) if found else None

    detail = await run_in_threadpool(run)
    if detail is None:
        raise NOT_FOUND
    return detail


@router.get("/{issue_id}/occurrences", response_model=OccurrenceList,
            summary="Occurrences (traces) of an issue, newest first")
async def list_occurrences(
    request: Request, tenant: Reader, issue_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50, cursor: str | None = None,
) -> OccurrenceList:
    engine = app_state(request).engine

    def run() -> OccurrenceList | None:
        with engine.connect() as conn:
            if not queries.issue_exists(conn, tenant, issue_id):
                return None
            items, next_cursor = queries.list_occurrences(conn, tenant, issue_id,
                                                          limit=limit, cursor=cursor)
        return OccurrenceList.model_validate({"items": items, "next_cursor": next_cursor})

    try:
        result = await run_in_threadpool(run)
    except queries.InvalidCursor as exc:
        raise ApiError(400, "invalid_cursor", str(exc)) from exc
    if result is None:
        raise NOT_FOUND
    return result


@router.post("/{issue_id}/status", response_model=IssueSummary,
             summary="Resolve, ignore or reopen an issue")
async def set_status(request: Request, tenant: Triager, issue_id: uuid.UUID,
                     change: StatusChange) -> IssueSummary:
    engine = app_state(request).engine

    def run() -> IssueSummary | None:
        with engine.begin() as conn:
            if not change_status(conn, tenant, issue_id, change.status, actor=tenant.actor,
                                 fixed_in_version=change.fixed_in_version, note=change.note):
                return None
            found = queries.get_issue(conn, tenant, issue_id)
        return IssueSummary.model_validate(found["issue"]) if found else None

    try:
        issue = await run_in_threadpool(run)
    except InvalidTransition as exc:
        raise ApiError(409, "invalid_transition", str(exc)) from exc
    if issue is None:
        raise NOT_FOUND
    return issue
