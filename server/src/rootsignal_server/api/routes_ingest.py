"""``POST /v1/traces``: OTLP/HTTP trace ingest."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from starlette.concurrency import run_in_threadpool

from rootsignal_server.api.deps import AppState, app_state, require_scope
from rootsignal_server.api.errors import ApiError
from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.ingest.normalize import IngestPolicy, normalize
from rootsignal_server.ingest.otlp import (
    OtlpDecodeError,
    PayloadTooLarge,
    decode_request,
    decompress,
    encode_response,
    media_type,
)
from rootsignal_server.ingest.service import (
    IngestResult,
    QuotaExceeded,
    charge_quota,
    load_project_policy,
    record_rejected_request,
    store_batch,
)

router = APIRouter()


async def _read_limited(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise PayloadTooLarge(f"request body exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise PayloadTooLarge(f"request body exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _ingest(state: AppState, tenant: TenantContext, request: ExportTraceServiceRequest,
            request_bytes: int) -> IngestResult:
    settings = state.settings
    with state.engine.begin() as conn:
        project = load_project_policy(conn, tenant)
    batch = normalize(request, IngestPolicy(
        max_payload_bytes=project.max_payload_bytes,
        redact_on_ingest=project.redact_on_ingest,  # type: ignore[arg-type]
        max_attribute_value_chars=settings.max_attribute_value_chars,
        max_span_attributes_bytes=settings.max_span_attributes_bytes,
        prices=state.prices,
    ))
    try:
        with state.engine.begin() as conn:
            charge_quota(conn, tenant, project, spans_received=batch.received,
                         spans_accepted=len(batch.spans), request_bytes=request_bytes)
            result = store_batch(conn, tenant, batch)
    except QuotaExceeded:
        with state.engine.begin() as conn:
            record_rejected_request(conn, tenant)
        raise
    return result


@router.post("/v1/traces", summary="Export traces (OTLP/HTTP)")
async def export_traces(
    request: Request,
    tenant: Annotated[TenantContext, Depends(require_scope("ingest"))],
) -> Response:
    state = app_state(request)
    media = media_type(request.headers.get("content-type"))
    if media is None:
        raise ApiError(415, "unsupported_media_type",
                       "use application/x-protobuf or application/json")
    try:
        raw = await _read_limited(request, state.settings.max_request_bytes)
        body = decompress(raw, request.headers.get("content-encoding"),
                          state.settings.max_decompressed_bytes)
        otlp_request = await run_in_threadpool(decode_request, body, media)
    except PayloadTooLarge as exc:
        raise ApiError(413, "payload_too_large", str(exc)) from exc
    except OtlpDecodeError as exc:
        raise ApiError(400, "invalid_request", str(exc)) from exc
    try:
        result = await run_in_threadpool(_ingest, state, tenant, otlp_request, len(raw))
    except QuotaExceeded as exc:
        raise ApiError(429, "quota_exceeded", str(exc),
                       {"Retry-After": str(exc.retry_after_s)}) from exc
    # Rejected spans are reported back to the exporter as OTLP partial success.
    return Response(encode_response(media, result.rejected, result.error_message),
                    media_type=media)
