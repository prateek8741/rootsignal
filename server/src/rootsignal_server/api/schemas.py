"""Response models for the read API (also the source of the OpenAPI document)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


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
    finding_count: int


class Context(BaseModel):
    org_slug: str
    org_name: str
    project_slug: str
    project_name: str
    environment: str
    scopes: list[str]
    key_prefix: str
    principal: str  # "api_key:<prefix>" or "session:<label>"
    llm_available: bool  # server has a model provider and this project has an LLM budget


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
    issue_id: uuid.UUID | None


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


# --- issues -------------------------------------------------------------------

class IssueSummary(BaseModel):
    id: uuid.UUID
    fingerprint: str
    components: dict[str, Any]
    detector: str
    agent_name: str | None
    tool_name: str | None
    error_type: str | None
    title: str
    description: str
    severity: str
    status: str
    is_regression: bool
    regression_count: int
    last_regressed_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    affected_versions: list[str]
    first_trace_id: str | None
    latest_trace_id: str | None
    worst_trace_id: str | None
    resolved_at: datetime | None
    fixed_in_version: str | None
    created_at: datetime
    updated_at: datetime


class IssueList(BaseModel):
    items: list[IssueSummary]
    next_cursor: str | None


class IssueVersion(BaseModel):
    agent_version: str | None
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime


class IssueEventOut(BaseModel):
    kind: str
    actor: str
    from_status: str | None
    to_status: str | None
    details: dict[str, Any]
    created_at: datetime


class OccurrenceOut(BaseModel):
    id: uuid.UUID
    trace_id: str
    finding_id: uuid.UUID
    agent_version: str | None
    severity: str
    occurred_at: datetime
    finding_title: str
    finding_summary: str


class OccurrenceList(BaseModel):
    items: list[OccurrenceOut]
    next_cursor: str | None


class IssueDetail(BaseModel):
    issue: IssueSummary
    versions: list[IssueVersion]
    events: list[IssueEventOut]
    recent_occurrences: list[OccurrenceOut]


class StatusChange(BaseModel):
    status: Literal["open", "resolved", "ignored"]
    fixed_in_version: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


# --- investigations -------------------------------------------------------------

class InvestigationRequest(BaseModel):
    use_llm: bool = False


class ClaimOut(BaseModel):
    kind: str
    source: str
    statement: str
    refs: list[dict[str, Any]]


class InvestigationSummary(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    status: str
    requested_by: str
    use_llm: bool
    llm_status: str
    llm_provider: str | None
    llm_model: str | None
    llm_input_tokens: int
    llm_output_tokens: int
    llm_error: str | None
    summary: str | None
    evidence_trace_ids: list[str]
    rejected_claims: list[dict[str, Any]]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class InvestigationDetail(InvestigationSummary):
    claims: list[ClaimOut]


class InvestigationList(BaseModel):
    items: list[InvestigationSummary]
