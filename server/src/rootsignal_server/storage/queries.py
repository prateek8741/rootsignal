"""Tenant-scoped read queries. Every function filters by project *and* environment.

A key only ever sees its own environment's data; anything else is reported as
not found, so callers can't probe for other tenants' IDs.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.auth.keys import TenantContext

_TRACE_COLUMNS = (
    "trace_id, root_span_id, service_name, agent_name, agent_version, prompt_version, "
    "conversation_id, status, source, started_at, ended_at, duration_ms, span_count, "
    "error_count, llm_call_count, tool_call_count, input_tokens, output_tokens, cost_usd, "
    "unpriced_llm_call_count, completed_at, completion_reason"
)
_SPAN_COLUMNS = (
    "span_id, parent_span_id, kind, name, status, status_message, error_type, started_at, "
    "ended_at, duration_ms, model, provider, input_tokens, output_tokens, cost_usd, tool_name, "
    "side_effect, args_hash, call_index, repeat_index, input_payload_hash, output_payload_hash, "
    "attributes, events, dialect"
)


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class TraceFilters:
    status: str | None = None
    agent_name: str | None = None
    since: datetime | None = None
    until: datetime | None = None


def _encode_cursor(started_at: datetime, trace_id: str) -> str:
    raw = json.dumps([started_at.isoformat(), trace_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        started, trace_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(started), str(trace_id)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise InvalidCursor("invalid cursor") from exc


def list_traces(conn: Connection, tenant: TenantContext, filters: TraceFilters, *,
                limit: int, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """Newest first, keyset-paginated on ``(started_at, trace_id)``."""
    clauses = ["project_id = :p", "environment_id = :e"]
    params: dict[str, Any] = {"p": tenant.project_id, "e": tenant.environment_id,
                              "limit": limit + 1}
    if filters.status:
        clauses.append("status = :status")
        params["status"] = filters.status
    if filters.agent_name:
        clauses.append("agent_name = :agent")
        params["agent"] = filters.agent_name
    if filters.since:
        clauses.append("started_at >= :since")
        params["since"] = filters.since
    if filters.until:
        clauses.append("started_at < :until")
        params["until"] = filters.until
    if cursor:
        params["c_started"], params["c_trace"] = _decode_cursor(cursor)
        clauses.append("(started_at, trace_id) < (:c_started, :c_trace)")
    rows = conn.execute(text(
        f"SELECT {_TRACE_COLUMNS} FROM traces WHERE {' AND '.join(clauses)} "
        "ORDER BY started_at DESC, trace_id DESC LIMIT :limit"), params).mappings().all()
    items = [dict(r) for r in rows[:limit]]
    next_cursor = (_encode_cursor(items[-1]["started_at"], items[-1]["trace_id"])
                   if len(rows) > limit else None)
    return items, next_cursor


def get_trace(conn: Connection, tenant: TenantContext,
              trace_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    params = {"p": tenant.project_id, "e": tenant.environment_id, "t": trace_id}
    trace = conn.execute(text(
        f"SELECT {_TRACE_COLUMNS} FROM traces "
        "WHERE project_id = :p AND environment_id = :e AND trace_id = :t"), params
    ).mappings().one_or_none()
    if trace is None:
        return None
    spans = conn.execute(text(
        f"SELECT {_SPAN_COLUMNS} FROM spans "
        "WHERE project_id = :p AND environment_id = :e AND trace_id = :t "
        "ORDER BY started_at, span_id"), params).mappings().all()
    return dict(trace), [dict(s) for s in spans]


_FINDING_COLUMNS = (
    "f.id, f.detector, f.detector_version, f.finding_key, f.severity, f.title, f.summary, "
    "f.evidence_span_ids, f.facts, f.tool_name, f.error_type, f.created_at, f.updated_at, "
    "o.issue_id"
)


def get_findings(conn: Connection, tenant: TenantContext, trace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        f"SELECT {_FINDING_COLUMNS} FROM findings f "
        "LEFT JOIN issue_occurrences o ON o.finding_id = f.id "
        "WHERE f.project_id = :p AND f.environment_id = :e AND f.trace_id = :t "
        "ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, f.detector, f.finding_key"),
        {"p": tenant.project_id, "e": tenant.environment_id, "t": trace_id}).mappings().all()
    return [dict(r) for r in rows]


def get_payload(conn: Connection, tenant: TenantContext, sha256: str) -> dict[str, Any] | None:
    """A payload is visible only if a span in the caller's environment references it."""
    row = conn.execute(text(
        "SELECT p.sha256, p.body, p.byte_size, p.truncated, p.content_type, p.redaction_state "
        "FROM payloads p WHERE p.project_id = :p AND p.sha256 = :h AND EXISTS ("
        "  SELECT 1 FROM spans s WHERE s.project_id = :p AND s.environment_id = :e "
        "  AND (s.input_payload_hash = :h OR s.output_payload_hash = :h))"),
        {"p": tenant.project_id, "e": tenant.environment_id, "h": sha256},
    ).mappings().one_or_none()
    return dict(row) if row else None
