"""Regression case API. Creating and editing need ``triage``; reading needs ``read``."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.deps import app_state, require_scope
from rootsignal_server.api.errors import ApiError
from rootsignal_server.api.schemas import CaseCreate, CaseDetail, CaseEditBody, CaseList
from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.regression import service
from rootsignal_server.regression.builder import CaseSourceError

router = APIRouter(prefix="/v1")
Reader = Annotated[TenantContext, Depends(require_scope("read"))]
Triager = Annotated[TenantContext, Depends(require_scope("triage"))]
CASE_NOT_FOUND = ApiError(404, "not_found", "regression case not found")


@router.post("/issues/{issue_id}/regression-cases", response_model=CaseDetail,
             status_code=201, summary="Create a regression case from an occurrence")
async def create(request: Request, tenant: Triager, issue_id: uuid.UUID,
                 body: CaseCreate) -> CaseDetail:
    engine = app_state(request).engine
    override = body.input if "input" in body.model_fields_set else service.UNSET

    def run() -> CaseDetail | None:
        with engine.begin() as conn:
            case_id = service.create_case(conn, tenant, issue_id, trace_id=body.trace_id,
                                          name=body.name, input_override=override)
            if case_id is None:
                return None
            return CaseDetail.model_validate(service.get_case(conn, tenant, case_id))

    try:
        created = await run_in_threadpool(run)
    except CaseSourceError as exc:
        raise ApiError(422, "invalid_source", str(exc)) from exc
    if created is None:
        raise ApiError(404, "not_found", "issue not found")
    return created


@router.get("/issues/{issue_id}/regression-cases", response_model=CaseList,
            summary="Regression cases of an issue")
async def list_for_issue(request: Request, tenant: Reader, issue_id: uuid.UUID) -> CaseList:
    engine = app_state(request).engine

    def run() -> CaseList:
        with engine.connect() as conn:
            return CaseList.model_validate(
                {"items": service.list_cases(conn, tenant, issue_id=issue_id)})

    return await run_in_threadpool(run)


@router.get("/regression-cases", response_model=CaseList, summary="All regression cases")
async def list_all(request: Request, tenant: Reader,
                   status: Annotated[str | None, Query(max_length=30)] = None) -> CaseList:
    engine = app_state(request).engine

    def run() -> CaseList:
        with engine.connect() as conn:
            return CaseList.model_validate(
                {"items": service.list_cases(conn, tenant, status=status)})

    return await run_in_threadpool(run)


@router.get("/regression-cases/{case_id}", response_model=CaseDetail,
            summary="A case with its current (or a given) version")
async def get_one(request: Request, tenant: Reader, case_id: uuid.UUID,
                  version: Annotated[int | None, Query(ge=1)] = None) -> CaseDetail:
    engine = app_state(request).engine

    def run() -> CaseDetail | None:
        with engine.connect() as conn:
            found = service.get_case(conn, tenant, case_id, version)
        return CaseDetail.model_validate(found) if found else None

    found = await run_in_threadpool(run)
    if found is None:
        raise CASE_NOT_FOUND
    return found


@router.get("/regression-cases/{case_id}/spec", summary="The case spec as a standalone document")
async def spec(request: Request, tenant: Reader, case_id: uuid.UUID,
               version: Annotated[int | None, Query(ge=1)] = None) -> dict[str, Any]:
    engine = app_state(request).engine

    def run() -> dict[str, Any] | None:
        with engine.connect() as conn:
            found = service.get_case(conn, tenant, case_id, version)
        return service.spec_document(found) if found else None

    found = await run_in_threadpool(run)
    if found is None:
        raise CASE_NOT_FOUND
    return found


@router.patch("/regression-cases/{case_id}", response_model=CaseDetail,
              summary="Edit a case (creates a new version)")
async def edit(request: Request, tenant: Triager, case_id: uuid.UUID,
               body: CaseEditBody) -> CaseDetail:
    engine = app_state(request).engine
    fields = body.model_fields_set
    change = service.CaseEdit(
        name=body.name,
        entrypoint=body.entrypoint if "entrypoint" in fields else service.UNSET,
        input=body.input if "input" in fields else service.UNSET,
        disabled_assertions=body.disabled_assertions, note=body.note)

    def run() -> CaseDetail | None:
        with engine.begin() as conn:
            if not service.edit_case(conn, tenant, case_id, change):
                return None
            return CaseDetail.model_validate(service.get_case(conn, tenant, case_id))

    try:
        edited = await run_in_threadpool(run)
    except service.InvalidEdit as exc:
        raise ApiError(422, "invalid_edit", str(exc)) from exc
    if edited is None:
        raise CASE_NOT_FOUND
    return edited
