"""Response models for the read API (also the source of the OpenAPI document)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class TraceSummary(BaseModel):
    trace_id: str
    root_span_id: str | None
    service_name: str | None
    agent_name: str | None
    agent_version: str | None
    prompt_version: str | None
    conversation_id: str | None
    status: str
    source: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: float | None
    span_count: int
    error_count: int
    llm_call_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    unpriced_llm_call_count: int
    completed_at: datetime | None
    completion_reason: str | None


class TraceList(BaseModel):
    items: list[TraceSummary]
    next_cursor: str | None


class SpanOut(BaseModel):
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    status: str
    status_message: str | None
    error_type: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: float | None
    model: str | None
    provider: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    tool_name: str | None
    side_effect: str | None
    args_hash: str | None
    call_index: int | None
    repeat_index: int | None
    input_payload_hash: str | None
    output_payload_hash: str | None
    attributes: dict[str, Any]
    events: list[dict[str, Any]]
    dialect: str


class FindingOut(BaseModel):
    id: uuid.UUID
    detector: str
    detector_version: int
    finding_key: str
    severity: str
    title: str
    summary: str
    evidence_span_ids: list[str]
    facts: dict[str, Any]
    tool_name: str | None
    error_type: str | None
    created_at: datetime
    updated_at: datetime


class TraceDetail(BaseModel):
    trace: TraceSummary
    spans: list[SpanOut]
    findings: list[FindingOut]


class PayloadOut(BaseModel):
    sha256: str
    body: str
    byte_size: int
    truncated: bool
    content_type: str
    redaction_state: str
