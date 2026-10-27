"""Persist a normalised batch for one tenant: quotas, upserts, roll-ups, job enqueue.

All writes for a request happen in one transaction. Spans are insert-only and
keyed by ``(project_id, trace_id, span_id)``, so a retried batch is a no-op.
Trace roll-ups are recomputed from the ``spans`` table rather than incremented,
which keeps them correct when a trace's spans arrive over several batches.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.types import Text

from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.ingest.normalize import NormalizedBatch, NormalizedPayload, NormalizedSpan
from rootsignal_server.storage.models import Job, Payload, Span, Trace

TRACE_COMPLETED = "trace_completed"


class QuotaExceeded(Exception):
    def __init__(self, message: str, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class IngestResult:
    accepted: int
    rejected: int
    traces_touched: int
    traces_completed: int
    error_message: str = ""  # OTLP partial-success message when spans were rejected


@dataclass(frozen=True)
class ProjectPolicy:
    max_spans_per_day: int
    max_ingest_bytes_per_day: int
    max_payload_bytes: int
    redact_on_ingest: str


def load_project_policy(conn: Connection, tenant: TenantContext) -> ProjectPolicy:
    row = conn.execute(
        text("SELECT max_spans_per_day, max_ingest_bytes_per_day, max_payload_bytes, "
             "redact_on_ingest FROM projects WHERE id = :p AND org_id = :o"),
        {"p": tenant.project_id, "o": tenant.org_id},
    ).one()
    return ProjectPolicy(*row)


def _seconds_until_utc_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((tomorrow - now).total_seconds()), 1)


def charge_quota(conn: Connection, tenant: TenantContext, policy: ProjectPolicy, *,
                 spans_received: int, spans_accepted: int, request_bytes: int,
                 today: date | None = None) -> None:
    """Add this request to today's usage, or raise ``QuotaExceeded`` (caller rolls back).

    The upsert takes a row lock on the project's usage row, so concurrent
    requests for one project serialise here and can't overshoot together.
    """
    now = datetime.now(UTC)
    row = conn.execute(text(
        "INSERT INTO usage_daily (project_id, day, org_id, requests, spans_received, "
        "spans_accepted, bytes_received) VALUES (:p, :d, :o, 1, :sr, :sa, :b) "
        "ON CONFLICT (project_id, day) DO UPDATE SET "
        "requests = usage_daily.requests + 1, "
        "spans_received = usage_daily.spans_received + EXCLUDED.spans_received, "
        "spans_accepted = usage_daily.spans_accepted + EXCLUDED.spans_accepted, "
        "bytes_received = usage_daily.bytes_received + EXCLUDED.bytes_received "
        "RETURNING spans_accepted, bytes_received"),
        {"p": tenant.project_id, "o": tenant.org_id, "d": today or now.date(),
         "sr": spans_received, "sa": spans_accepted, "b": request_bytes},
    ).one()
    if row.spans_accepted > policy.max_spans_per_day:
        raise QuotaExceeded(f"daily span quota of {policy.max_spans_per_day} exceeded",
                            _seconds_until_utc_midnight(now))
    if row.bytes_received > policy.max_ingest_bytes_per_day:
        raise QuotaExceeded(f"daily ingest quota of {policy.max_ingest_bytes_per_day} bytes "
                            "exceeded", _seconds_until_utc_midnight(now))


def record_rejected_request(conn: Connection, tenant: TenantContext,
                            today: date | None = None) -> None:
    conn.execute(text(
        "INSERT INTO usage_daily (project_id, day, org_id, rejected_requests) "
        "VALUES (:p, :d, :o, 1) ON CONFLICT (project_id, day) DO UPDATE SET "
        "rejected_requests = usage_daily.rejected_requests + 1"),
        {"p": tenant.project_id, "o": tenant.org_id, "d": today or datetime.now(UTC).date()})


def _payload_rows(tenant: TenantContext, spans: Sequence[NormalizedSpan]) -> list[dict[str, Any]]:
    unique: dict[str, NormalizedPayload] = {}
    for span in spans:
        for payload in (span.input_payload, span.output_payload):
            if payload is not None:
                unique.setdefault(payload.sha256, payload)
    return [{"project_id": tenant.project_id, "org_id": tenant.org_id, "sha256": p.sha256,
             "body": p.body, "byte_size": p.byte_size, "truncated": p.truncated,
             "content_type": p.content_type, "redaction_state": p.redaction_state}
            for p in unique.values()]


def _trace_rows(tenant: TenantContext, spans: Sequence[NormalizedSpan]) -> list[dict[str, Any]]:
    first: dict[str, dict[str, Any]] = {}
    for span in spans:
        row = first.get(span.trace_id)
        if row is None:
            first[span.trace_id] = {
                "project_id": tenant.project_id, "trace_id": span.trace_id,
                "org_id": tenant.org_id, "environment_id": tenant.environment_id,
                "started_at": span.started_at, "service_name": span.service_name,
                # Recorded as claimed; the run's tenant is checked at evaluation time.
                "source": "replay" if span.replay_run_id else "production",
                "replay_run_id": span.replay_run_id}
        elif span.started_at < row["started_at"]:
            row["started_at"] = span.started_at
    return list(first.values())


def _span_rows(tenant: TenantContext, spans: Sequence[NormalizedSpan]) -> list[dict[str, Any]]:
    return [{
        "project_id": tenant.project_id, "org_id": tenant.org_id,
        "environment_id": tenant.environment_id, "trace_id": s.trace_id, "span_id": s.span_id,
        "parent_span_id": s.parent_span_id, "kind": s.kind, "name": s.name, "status": s.status,
        "status_message": s.status_message, "error_type": s.error_type,
        "started_at": s.started_at, "ended_at": s.ended_at, "duration_ms": s.duration_ms,
        "model": s.model, "provider": s.provider, "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens, "cost_usd": s.cost_usd, "tool_name": s.tool_name,
        "side_effect": s.side_effect, "args_hash": s.args_hash, "call_index": s.call_index,
        "repeat_index": s.repeat_index,
        "input_payload_hash": s.input_payload.sha256 if s.input_payload else None,
        "output_payload_hash": s.output_payload.sha256 if s.output_payload else None,
        "attributes": s.attributes, "events": s.events, "dialect": s.dialect,
    } for s in spans]


_RECOMPUTE_TRACES = text("""
WITH agg AS (
    SELECT trace_id,
           count(*) AS span_count,
           count(*) FILTER (WHERE status = 'error') AS error_count,
           count(*) FILTER (WHERE kind = 'llm') AS llm_call_count,
           -- A third-party tool span duplicating an SDK tool span counts once
           -- (same rule as detection.model.shadowed_ids).
           count(*) FILTER (WHERE kind IN ('tool', 'mcp') AND NOT (dialect <> 'rootsignal'
               AND EXISTS (SELECT 1 FROM spans r WHERE r.project_id = spans.project_id
                   AND r.trace_id = spans.trace_id AND r.dialect = 'rootsignal'
                   AND r.kind IN ('tool', 'mcp') AND r.tool_name = spans.tool_name
                   AND r.started_at BETWEEN spans.started_at
                                        AND coalesce(spans.ended_at, spans.started_at))))
               AS tool_call_count,
           coalesce(sum(input_tokens), 0) AS input_tokens,
           coalesce(sum(output_tokens), 0) AS output_tokens,
           sum(cost_usd) AS cost_usd,
           count(*) FILTER (WHERE kind = 'llm' AND cost_usd IS NULL) AS unpriced
    FROM spans WHERE project_id = :p AND trace_id = ANY(:ids)
    GROUP BY trace_id
), root AS (
    SELECT DISTINCT ON (trace_id) trace_id, span_id, status, ended_at, duration_ms
    FROM spans WHERE project_id = :p AND trace_id = ANY(:ids) AND parent_span_id IS NULL
    ORDER BY trace_id, started_at
), agent AS (
    SELECT DISTINCT ON (trace_id) trace_id,
           attributes ->> 'gen_ai.agent.name' AS agent_name,
           attributes ->> 'gen_ai.agent.version' AS agent_version,
           attributes ->> 'rootsignal.prompt.version' AS prompt_version,
           attributes ->> 'gen_ai.conversation.id' AS conversation_id
    FROM spans WHERE project_id = :p AND trace_id = ANY(:ids) AND kind = 'agent'
    ORDER BY trace_id, started_at
)
UPDATE traces t SET
    span_count = agg.span_count,
    error_count = agg.error_count,
    llm_call_count = agg.llm_call_count,
    tool_call_count = agg.tool_call_count,
    input_tokens = agg.input_tokens,
    output_tokens = agg.output_tokens,
    cost_usd = agg.cost_usd,
    unpriced_llm_call_count = agg.unpriced,
    root_span_id = root.span_id,
    status = coalesce(root.status, 'unset'),
    ended_at = root.ended_at,
    duration_ms = root.duration_ms,
    -- Completed by the root span arriving, or earlier by the worker's idle
    -- sweep. Late child spans without a root must keep an idle completion.
    completed_at = CASE WHEN root.span_id IS NOT NULL
                        THEN coalesce(t.completed_at, now()) ELSE t.completed_at END,
    completion_reason = CASE WHEN root.span_id IS NOT NULL THEN 'root_span'
                             ELSE t.completion_reason END,
    agent_name = agent.agent_name,
    agent_version = agent.agent_version,
    prompt_version = agent.prompt_version,
    conversation_id = agent.conversation_id,
    updated_at = now()
FROM agg
LEFT JOIN root ON root.trace_id = agg.trace_id
LEFT JOIN agent ON agent.trace_id = agg.trace_id
WHERE t.project_id = :p AND t.trace_id = agg.trace_id
RETURNING t.trace_id, t.completed_at IS NOT NULL AS completed
""").bindparams(bindparam("ids", type_=ARRAY(Text)))


def store_batch(conn: Connection, tenant: TenantContext, batch: NormalizedBatch) -> IngestResult:
    """Write the batch's spans. The caller owns the transaction."""
    spans = batch.spans
    message = batch.error_message() if batch.rejected else ""
    if not spans:
        return IngestResult(0, batch.rejected, 0, 0, message)

    payload_rows = _payload_rows(tenant, spans)
    if payload_rows:
        stmt = insert(Payload).values(payload_rows)
        conn.execute(stmt.on_conflict_do_update(
            index_elements=["project_id", "sha256"],
            set_={"last_referenced_at": text("now()")}))

    trace_stmt = insert(Trace).values(_trace_rows(tenant, spans))
    conn.execute(trace_stmt.on_conflict_do_update(
        index_elements=["project_id", "trace_id"],
        set_={"started_at": text("LEAST(traces.started_at, excluded.started_at)"),
              "service_name": text("coalesce(traces.service_name, excluded.service_name)"),
              "replay_run_id": text("coalesce(traces.replay_run_id, excluded.replay_run_id)"),
              "source": text("CASE WHEN coalesce(traces.replay_run_id, "
                             "excluded.replay_run_id) IS NULL THEN traces.source "
                             "ELSE 'replay' END")}))

    conn.execute(insert(Span).values(_span_rows(tenant, spans)).on_conflict_do_nothing(
        index_elements=["project_id", "trace_id", "span_id"]))

    trace_ids = sorted({s.trace_id for s in spans})
    updated = conn.execute(_RECOMPUTE_TRACES, {"p": tenant.project_id, "ids": trace_ids}).all()
    completed = [row.trace_id for row in updated if row.completed]
    if completed:
        jobs = [{"org_id": tenant.org_id, "project_id": tenant.project_id,
                 "kind": TRACE_COMPLETED, "payload": {"trace_id": trace_id},
                 "dedupe_key": f"{TRACE_COMPLETED}:{tenant.project_id}:{trace_id}"}
                for trace_id in completed]
        # A trace that gets late spans after detection ran is enqueued again;
        # the partial unique index only prevents duplicates of active jobs.
        conn.execute(insert(Job).values(jobs).on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text("status IN ('queued', 'running')")))
    return IngestResult(len(spans), batch.rejected, len(trace_ids), len(completed), message)
