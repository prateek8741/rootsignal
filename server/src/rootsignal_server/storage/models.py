"""Canonical relational schema. See docs/TRACE_SCHEMA.md.

Tenant rules: every tenant-owned row carries ``org_id`` and ``project_id``, and
the primary keys of ``traces``, ``spans`` and ``payloads`` start with
``project_id``, so IDs supplied by clients (trace/span IDs, content hashes) can
never collide with, or overwrite, another tenant's rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SPAN_KINDS = ("agent", "llm", "tool", "retrieval", "mcp", "chain", "other")
SPAN_STATUSES = ("unset", "ok", "error")
SIDE_EFFECTS = ("none", "read", "write", "unknown")
TRACE_SOURCES = ("production", "replay")
DIALECTS = ("rootsignal", "gen_ai", "openinference", "openllmetry", "unknown")
REDACTION_STATES = ("none", "sdk", "server", "unknown")
JOB_STATUSES = ("queued", "running", "done", "failed", "dead")
REDACT_ON_INGEST = ("always", "if_not_sdk", "never")
COMPLETION_REASONS = ("root_span", "idle_timeout")
SEVERITIES = ("critical", "high", "medium", "low")
ISSUE_STATUSES = ("open", "resolved", "ignored")
ISSUE_EVENT_KINDS = ("created", "status_changed", "regressed", "straggler", "investigated",
                     "replayed")
SESSION_SCOPES = ("read", "triage")
INVESTIGATION_STATUSES = ("queued", "running", "completed", "failed")
CLAIM_KINDS = ("observed", "hypothesis", "alternative", "missing_evidence", "next_step", "verified")
CLAIM_SOURCES = ("rules", "llm", "replay")
# draft: created, not yet replayed. The others are set by replay (increment 8).
CASE_STATUSES = ("draft", "reproduced", "not_reproduced", "passing", "failing", "archived")
REPLAY_PURPOSES = ("reproduce", "verify")
REPLAY_MODES = ("recorded", "live_model")
REPLAY_MATCHING = ("by_args", "strict_order")
REPLAY_STATUSES = ("running", "awaiting_trace", "evaluated", "error")
REPLAY_VERDICTS = ("failing", "passing", "broken", "diverged", "inconclusive", "crashed")
API_KEY_SCOPES = ("ingest", "read", "triage", "replay", "admin")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True,
                         server_default=text("gen_random_uuid()"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _org_id() -> Mapped[uuid.UUID]:
    """Denormalised tenant column; consistency is enforced by :func:`_tenant_fks`."""
    return mapped_column(UUID(as_uuid=True), nullable=False)


def _tenant_fks(*, environment: bool) -> tuple[ForeignKeyConstraint, ...]:
    """Composite FKs that make the database reject rows whose org, project and
    environment don't belong together, so an ingest bug can't mix tenants."""
    fks = [ForeignKeyConstraint(["org_id", "project_id"], ["projects.org_id", "projects.id"],
                                ondelete="CASCADE")]
    if environment:
        fks.append(ForeignKeyConstraint(["project_id", "environment_id"],
                                        ["environments.project_id", "environments.id"],
                                        ondelete="CASCADE"))
    return tuple(fks)


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------

class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("org_id", "slug"),
        UniqueConstraint("org_id", "id"),  # target of tenant composite FKs
        CheckConstraint("retention_days > 0", name="retention_positive"),
        CheckConstraint("max_spans_per_day > 0 AND max_ingest_bytes_per_day > 0 "
                        "AND max_payload_bytes >= 1024", name="quotas_positive"),
        CheckConstraint(_in("redact_on_ingest", REDACT_ON_INGEST), name="redact_on_ingest"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="14")
    # Quotas and ingest policy; see docs/COST_MODEL.md for how defaults were chosen.
    max_spans_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="100000")
    max_ingest_bytes_per_day: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=str(1024**3))
    max_payload_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="65536")
    # Server-side redaction: always, only when the SDK didn't redact, or never.
    redact_on_ingest: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="if_not_sdk")
    # Per-detector overrides of the defaults in detection/config.py.
    detector_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Per-detector fingerprint components overriding issues/fingerprint.py.
    issue_grouping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Platform LLM tokens (input + output) per calendar month; 0 disables LLM use.
    llm_monthly_token_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _created_at()


class Environment(Base):
    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        UniqueConstraint("project_id", "id"),  # target of tenant composite FKs
        *_tenant_fks(environment=False),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class ApiKey(Base):
    """Only a SHA-256 of the key is stored; the plaintext is shown once at creation."""

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "scopes <@ ARRAY[" + ", ".join(repr(s) for s in API_KEY_SCOPES) + "]::text[]",
            name="known_scopes"),
        CheckConstraint("cardinality(scopes) > 0", name="has_scope"),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY['ingest']::text[]"))
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------
# Traces
# --------------------------------------------------------------------------

class Trace(Base):
    __tablename__ = "traces"
    __table_args__ = (
        CheckConstraint("char_length(trace_id) = 32", name="trace_id_len"),
        CheckConstraint(_in("status", SPAN_STATUSES), name="status"),
        CheckConstraint(_in("source", TRACE_SOURCES), name="source"),
        CheckConstraint(
            f"completion_reason IS NULL OR {_in('completion_reason', COMPLETION_REASONS)}",
            name="completion_reason"),
        Index("ix_traces_incomplete", "updated_at",
              postgresql_where=text("completed_at IS NULL")),
        Index("ix_traces_project_env_started", "project_id", "environment_id",
              text("started_at DESC")),
        Index("ix_traces_project_agent_started", "project_id", "agent_name",
              text("started_at DESC")),
        Index("ix_traces_project_errors", "project_id", text("started_at DESC"),
              postgresql_where=text("status = 'error'")),
        *_tenant_fks(environment=True),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    root_span_id: Mapped[str | None] = mapped_column(Text)
    service_name: Mapped[str | None] = mapped_column(Text)
    agent_name: Mapped[str | None] = mapped_column(Text)
    agent_version: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    conversation_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unset")
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="production")
    replay_case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Set from the replay child's resource attributes; verified against the run's tenant.
    replay_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Double)
    span_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    llm_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # Model calls with no price: cost_usd is a lower bound when this is > 0.
    unpriced_llm_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # root_span: the root span arrived; idle_timeout: no spans for a while, root never seen.
    completion_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "trace_id"],
                             ["traces.project_id", "traces.trace_id"], ondelete="CASCADE"),
        CheckConstraint("char_length(span_id) = 16", name="span_id_len"),
        CheckConstraint(_in("kind", SPAN_KINDS), name="kind"),
        CheckConstraint(_in("status", SPAN_STATUSES), name="status"),
        CheckConstraint(f"side_effect IS NULL OR {_in('side_effect', SIDE_EFFECTS)}",
                        name="side_effect"),
        CheckConstraint(_in("dialect", DIALECTS), name="dialect"),
        Index("ix_spans_project_trace_started", "project_id", "trace_id", "started_at"),
        Index("ix_spans_project_tool_started", "project_id", "tool_name", "started_at",
              postgresql_where=text("kind = 'tool'")),
        Index("ix_spans_project_errors", "project_id", "started_at",
              postgresql_where=text("status = 'error'")),
        # Payload visibility checks look spans up by payload hash.
        Index("ix_spans_project_input_payload", "project_id", "input_payload_hash",
              postgresql_where=text("input_payload_hash IS NOT NULL")),
        Index("ix_spans_project_output_payload", "project_id", "output_payload_hash",
              postgresql_where=text("output_payload_hash IS NOT NULL")),
        *_tenant_fks(environment=True),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    span_id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unset")
    status_message: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Double)
    model: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    tool_name: Mapped[str | None] = mapped_column(Text)
    side_effect: Mapped[str | None] = mapped_column(Text)
    args_hash: Mapped[str | None] = mapped_column(Text)
    call_index: Mapped[int | None] = mapped_column(Integer)
    repeat_index: Mapped[int | None] = mapped_column(Integer)
    # References into ``payloads`` (same project). Not a foreign key so payload
    # retention can delete bodies independently; readers treat a miss as "expired".
    input_payload_hash: Mapped[str | None] = mapped_column(Text)
    output_payload_hash: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    events: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    dialect: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    created_at: Mapped[datetime] = _created_at()


class Payload(Base):
    """Content-addressed payload bodies, deduplicated within a project only."""

    __tablename__ = "payloads"
    __table_args__ = (
        CheckConstraint("char_length(sha256) = 64", name="sha256_len"),
        CheckConstraint(_in("redaction_state", REDACTION_STATES), name="redaction_state"),
        CheckConstraint("content_type IN ('text', 'json')", name="content_type"),
        CheckConstraint("byte_size >= 0", name="byte_size"),
        Index("ix_payloads_project_last_referenced", "project_id", "last_referenced_at"),
        *_tenant_fks(environment=False),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    redaction_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="text")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    last_referenced_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

class Finding(Base):
    """One detector hit on one trace. Grouped into issues in increment 4.

    ``finding_key`` distinguishes several hits of one detector in a trace (e.g.
    two different tools looping); with the trace it makes re-detection
    idempotent.
    """

    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("project_id", "trace_id", "detector", "finding_key"),
        UniqueConstraint("project_id", "id"),  # target of tenant composite FKs
        ForeignKeyConstraint(["project_id", "trace_id"],
                             ["traces.project_id", "traces.trace_id"], ondelete="CASCADE"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        Index("ix_findings_project_detector_created", "project_id", "detector",
              text("created_at DESC")),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    detector: Mapped[str] = mapped_column(Text, nullable=False)
    detector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_key: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_span_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Denormalised grouping fields for issue fingerprints (increment 4).
    agent_name: Mapped[str | None] = mapped_column(Text)
    agent_version: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------

class Issue(Base):
    """A group of findings sharing a fingerprint, within one environment.

    Aggregates (counts, first/last seen, versions, representative traces) are
    recomputed from ``issue_occurrences`` rather than incremented.
    """

    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("project_id", "environment_id", "fingerprint"),
        UniqueConstraint("project_id", "id"),  # target of tenant composite FKs
        CheckConstraint(_in("status", ISSUE_STATUSES), name="status"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        CheckConstraint("occurrence_count >= 0 AND regression_count >= 0", name="counts"),
        Index("ix_issues_project_env_status_last_seen", "project_id", "environment_id",
              "status", text("last_seen_at DESC")),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    detector: Mapped[str] = mapped_column(Text, nullable=False)
    agent_name: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    is_regression: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"))
    regression_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_regressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    affected_versions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]"))
    first_trace_id: Mapped[str | None] = mapped_column(Text)
    latest_trace_id: Mapped[str | None] = mapped_column(Text)
    worst_trace_id: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fixed_in_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class IssueOccurrence(Base):
    """Links one finding (and so one trace) to its issue. One row per finding."""

    __tablename__ = "issue_occurrences"
    __table_args__ = (
        UniqueConstraint("finding_id"),
        ForeignKeyConstraint(["project_id", "issue_id"], ["issues.project_id", "issues.id"],
                             ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "finding_id"],
                             ["findings.project_id", "findings.id"], ondelete="CASCADE"),
        Index("ix_issue_occurrences_issue_occurred", "issue_id", text("occurred_at DESC")),
        Index("ix_issue_occurrences_project_trace", "project_id", "trace_id"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    issue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    finding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    # How bad this occurrence is relative to others (evidence size); picks the "worst" trace.
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class IssueEvent(Base):
    """Issue history: creation, status changes (who/why), regressions."""

    __tablename__ = "issue_events"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "issue_id"], ["issues.project_id", "issues.id"],
                             ondelete="CASCADE"),
        CheckConstraint(_in("kind", ISSUE_EVENT_KINDS), name="kind"),
        Index("ix_issue_events_issue_created", "issue_id", "created_at"),
        *_tenant_fks(environment=False),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    issue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------
# Dashboard sessions
# --------------------------------------------------------------------------

class DashboardSession(Base):
    """Server-side browser session. The browser holds only an httpOnly cookie
    with the random token; we store its SHA-256."""

    __tablename__ = "dashboard_sessions"
    __table_args__ = (
        CheckConstraint(
            "scopes <@ ARRAY[" + ", ".join(repr(s) for s in SESSION_SCOPES) + "]::text[]",
            name="session_scopes"),
        CheckConstraint("cardinality(scopes) > 0", name="has_scope"),
        CheckConstraint("created_via IN ('login_link', 'api_key')", name="created_via"),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    # Set when created by exchanging a key: revoking that key ends the session.
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"))
    created_via: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginLink(Base):
    """Single-use, short-lived token that becomes a dashboard session."""

    __tablename__ = "login_links"
    __table_args__ = (
        CheckConstraint(
            "scopes <@ ARRAY[" + ", ".join(repr(s) for s in SESSION_SCOPES) + "]::text[]",
            name="link_scopes"),
        CheckConstraint("cardinality(scopes) > 0", name="has_scope"),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------
# Investigations
# --------------------------------------------------------------------------

class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "issue_id"], ["issues.project_id", "issues.id"],
                             ondelete="CASCADE"),
        CheckConstraint(_in("status", INVESTIGATION_STATUSES), name="status"),
        Index("ix_investigations_issue_created", "issue_id", text("created_at DESC")),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    issue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    use_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # What happened with the LLM step: not_requested | disabled | budget_exhausted |
    # completed | failed. The deterministic part runs regardless.
    llm_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="not_requested")
    llm_provider: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    llm_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    llm_error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    evidence_trace_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]"))
    # Snapshot of exactly what the claims (and any LLM prompt) were based on.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Claims dropped by validation, with reasons (e.g. cited span not in evidence).
    rejected_claims: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvestigationClaim(Base):
    __tablename__ = "investigation_claims"
    __table_args__ = (
        CheckConstraint(_in("kind", CLAIM_KINDS), name="kind"),
        CheckConstraint(_in("source", CLAIM_SOURCES), name="source"),
        # Only deterministic rules state observations; only replay may verify.
        CheckConstraint("kind <> 'observed' OR source = 'rules'", name="observed_by_rules"),
        CheckConstraint("kind <> 'verified' OR source = 'replay'", name="verified_by_replay"),
        Index("ix_investigation_claims_investigation", "investigation_id", "position"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"))


# --------------------------------------------------------------------------
# Regression cases
# --------------------------------------------------------------------------

class RegressionCase(Base):
    """A failing production run captured as a repeatable test. Content lives in
    immutable ``regression_case_versions``; this row tracks identity and status."""

    __tablename__ = "regression_cases"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),  # target of tenant composite FKs
        ForeignKeyConstraint(["project_id", "issue_id"], ["issues.project_id", "issues.id"],
                             ondelete="CASCADE"),
        CheckConstraint(_in("status", CASE_STATUSES), name="status"),
        CheckConstraint("current_version >= 1", name="version"),
        Index("ix_regression_cases_issue", "issue_id"),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    issue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Not a foreign key: the case must outlive the trace (retention).
    source_trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class RegressionCaseVersion(Base):
    """Immutable snapshot of a case's spec (input, fixtures, assertions, entrypoint)."""

    __tablename__ = "regression_case_versions"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "case_id"],
                             ["regression_cases.project_id", "regression_cases.id"],
                             ondelete="CASCADE"),
        CheckConstraint("version >= 1", name="version"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class ReplayRun(Base):
    """One execution of a case version against agent code, and its verdict."""

    __tablename__ = "replay_runs"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "case_id"],
                             ["regression_cases.project_id", "regression_cases.id"],
                             ondelete="CASCADE"),
        CheckConstraint(_in("purpose", REPLAY_PURPOSES), name="purpose"),
        CheckConstraint(_in("mode", REPLAY_MODES), name="mode"),
        CheckConstraint(_in("matching", REPLAY_MATCHING), name="matching"),
        CheckConstraint(_in("status", REPLAY_STATUSES), name="status"),
        CheckConstraint(f"verdict IS NULL OR {_in('verdict', REPLAY_VERDICTS)}", name="verdict"),
        Index("ix_replay_runs_case_created", "case_id", text("created_at DESC")),
        *_tenant_fks(environment=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = _org_id()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    matching: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    verdict: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text)
    # What the replay child observed: served/stubbed calls, divergences, blocked network.
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    results: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _created_at()
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------
# Usage accounting and audit
# --------------------------------------------------------------------------

class UsageDaily(Base):
    """Per-project, per-UTC-day ingest counters used for quotas and cost tracking."""

    __tablename__ = "usage_daily"
    __table_args__ = (*_tenant_fks(environment=False),)

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_id()
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    spans_received: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    spans_accepted: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rejected_requests: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0")
    llm_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    llm_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0")


class AuditEvent(Base):
    """Append-only record of security-relevant actions (key creation, revocation, ...)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("project_id IS NULL OR org_id IS NOT NULL", name="tenant_pair"),
        ForeignKeyConstraint(["org_id", "project_id"], ["projects.org_id", "projects.id"],
                             ondelete="CASCADE"),
        Index("ix_audit_events_org_created", "org_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------

class Job(Base):
    """Durable queue; workers claim rows with ``FOR UPDATE SKIP LOCKED``."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="attempts"),
        Index("ix_jobs_claimable", text("priority DESC"), "run_after",
              postgresql_where=text("status = 'queued'")),
        Index("ix_jobs_dedupe_active", "dedupe_key", unique=True,
              postgresql_where=text("status IN ('queued', 'running')")),
        CheckConstraint("(org_id IS NULL) = (project_id IS NULL)", name="tenant_pair"),
        *_tenant_fks(environment=False),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # NULL for system jobs; otherwise both set and consistent (composite FK).
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    run_after: Mapped[datetime] = _created_at()
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
