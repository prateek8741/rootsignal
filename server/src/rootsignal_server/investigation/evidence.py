"""Collect the evidence for an issue. Read-only, tenant-scoped SQL; no LLM.

Every query is filtered by the investigation's project and environment, so
nothing from another tenant can reach the rules or a prompt.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.investigation.model import (
    EvidenceBundle,
    FindingEvidence,
    SpanEvidence,
    TraceEvidence,
)

MAX_SPANS_PER_TRACE = 60
MAX_MESSAGE_CHARS = 300
# Traffic and dependency comparisons look this far around the issue's lifetime.
WINDOW_PADDING = timedelta(hours=1)


def _short(text_value: str | None) -> str | None:
    if text_value is None:
        return None
    return text_value if len(text_value) <= MAX_MESSAGE_CHARS else (
        text_value[:MAX_MESSAGE_CHARS] + "…")


def collect(conn: Connection, project_id: uuid.UUID, environment_id: uuid.UUID,
            issue_id: uuid.UUID) -> EvidenceBundle | None:
    issue = conn.execute(text(
        "SELECT id, title, detector, components, severity, status, occurrence_count, "
        "first_seen_at, last_seen_at, affected_versions, regression_count, fixed_in_version, "
        "agent_name, tool_name, error_type, first_trace_id, latest_trace_id, worst_trace_id "
        "FROM issues WHERE id = :i AND project_id = :p AND environment_id = :e"),
        {"i": issue_id, "p": project_id, "e": environment_id}).mappings().one_or_none()
    if issue is None:
        return None
    scope = {"p": project_id, "e": environment_id}

    roles: dict[str, list[str]] = {}
    for role in ("worst", "latest", "first"):
        trace_id = issue[f"{role}_trace_id"]
        if trace_id:
            roles.setdefault(trace_id, []).append(role)
    traces = [t for t in (_trace(conn, scope, trace_id, issue_id, r)
                          for trace_id, r in roles.items()) if t is not None]

    window_start = issue["first_seen_at"] - WINDOW_PADDING
    window_end = issue["last_seen_at"] + WINDOW_PADDING
    stats: dict[str, Any] = {
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "version_breakdown": _versions(conn, scope, issue_id, issue["agent_name"],
                                       window_start, window_end),
    }
    if issue["tool_name"]:
        stats["tool_callers"] = _tool_callers(conn, scope, issue["tool_name"], window_start,
                                              window_end)
    return EvidenceBundle(issue={
        "id": str(issue["id"]), "title": issue["title"], "detector": issue["detector"],
        "components": issue["components"], "severity": issue["severity"],
        "status": issue["status"], "occurrence_count": issue["occurrence_count"],
        "first_seen_at": issue["first_seen_at"].isoformat(),
        "last_seen_at": issue["last_seen_at"].isoformat(),
        "affected_versions": list(issue["affected_versions"]),
        "regression_count": issue["regression_count"],
        "fixed_in_version": issue["fixed_in_version"], "agent_name": issue["agent_name"],
        "tool_name": issue["tool_name"], "error_type": issue["error_type"],
    }, traces=traces, stats=stats)


def _trace(conn: Connection, scope: dict[str, Any], trace_id: str, issue_id: uuid.UUID,
           roles: list[str]) -> TraceEvidence | None:
    head = conn.execute(text(
        "SELECT trace_id, agent_name, agent_version, started_at, status, completion_reason "
        "FROM traces WHERE project_id = :p AND environment_id = :e AND trace_id = :t"),
        {**scope, "t": trace_id}).mappings().one_or_none()
    if head is None:
        return None
    rows = conn.execute(text(
        "SELECT span_id, kind, name, tool_name, model, status, error_type, status_message, "
        "duration_ms, repeat_index, side_effect, input_payload_hash IS NOT NULL AS input_captured "
        "FROM spans WHERE project_id = :p AND environment_id = :e AND trace_id = :t "
        "ORDER BY started_at, span_id LIMIT :n"),
        {**scope, "t": trace_id, "n": MAX_SPANS_PER_TRACE + 1}).mappings().all()
    spans = [SpanEvidence(
        span_id=r["span_id"], kind=r["kind"], name=r["name"], tool_name=r["tool_name"],
        model=r["model"], status=r["status"], error_type=r["error_type"],
        status_message=_short(r["status_message"]), duration_ms=r["duration_ms"],
        repeat_index=r["repeat_index"], side_effect=r["side_effect"],
        input_captured=r["input_captured"]) for r in rows[:MAX_SPANS_PER_TRACE]]
    findings = conn.execute(text(
        "SELECT f.id, f.detector, f.title, f.summary, f.evidence_span_ids, f.facts "
        "FROM findings f JOIN issue_occurrences o ON o.finding_id = f.id "
        "WHERE o.issue_id = :i AND f.project_id = :p AND f.environment_id = :e "
        "AND f.trace_id = :t ORDER BY f.detector, f.finding_key"),
        {**scope, "i": issue_id, "t": trace_id}).mappings().all()
    return TraceEvidence(
        trace_id=trace_id, roles=roles, agent_name=head["agent_name"],
        agent_version=head["agent_version"], started_at=head["started_at"].isoformat(),
        status=head["status"], completion_reason=head["completion_reason"], spans=spans,
        findings=[FindingEvidence(str(f["id"]), f["detector"], f["title"], f["summary"],
                                  list(f["evidence_span_ids"]), dict(f["facts"]))
                  for f in findings],
        spans_truncated=len(rows) > MAX_SPANS_PER_TRACE)


def _versions(conn: Connection, scope: dict[str, Any], issue_id: uuid.UUID,
              agent_name: str | None, start: Any, end: Any) -> list[dict[str, Any]]:
    """Per agent version: traces in the window and how many hit this issue."""
    rows = conn.execute(text(
        "SELECT t.agent_version, count(*) AS traces, "
        "count(*) FILTER (WHERE EXISTS (SELECT 1 FROM issue_occurrences o "
        "  WHERE o.issue_id = :i AND o.trace_id = t.trace_id)) AS affected "
        "FROM traces t WHERE t.project_id = :p AND t.environment_id = :e "
        "AND t.source = 'production' AND t.started_at BETWEEN :start AND :end "
        "AND (CAST(:agent AS text) IS NULL OR t.agent_name = :agent) "
        "GROUP BY t.agent_version ORDER BY t.agent_version NULLS LAST"),
        {**scope, "i": issue_id, "agent": agent_name, "start": start, "end": end}).mappings()
    return [{"agent_version": r["agent_version"], "traces": r["traces"],
             "affected": r["affected"]} for r in rows]


def _tool_callers(conn: Connection, scope: dict[str, Any], tool: str, start: Any,
                  end: Any) -> list[dict[str, Any]]:
    """How the same tool behaved for every agent calling it in the window."""
    rows = conn.execute(text(
        "SELECT t.agent_name, count(*) AS calls, "
        "count(*) FILTER (WHERE s.status = 'error') AS errors, "
        "array_agg(DISTINCT s.error_type) FILTER (WHERE s.error_type IS NOT NULL) AS error_types "
        "FROM spans s JOIN traces t ON t.project_id = s.project_id AND t.trace_id = s.trace_id "
        "WHERE s.project_id = :p AND s.environment_id = :e AND s.kind IN ('tool', 'mcp') "
        "AND s.tool_name = :tool AND s.started_at BETWEEN :start AND :end "
        "AND t.source = 'production' GROUP BY t.agent_name ORDER BY t.agent_name NULLS LAST"),
        {**scope, "tool": tool, "start": start, "end": end}).mappings()
    return [{"agent_name": r["agent_name"], "calls": r["calls"], "errors": r["errors"],
             "error_types": sorted(r["error_types"] or [])} for r in rows]
