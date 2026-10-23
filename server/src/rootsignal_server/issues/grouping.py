"""Group a trace's stored findings into issues.

Runs in the detection job's transaction, after findings are saved. For each
finding: fingerprint → upsert issue → upsert occurrence → apply the regression
rule → recompute aggregates of every issue touched (including issues the
trace *used* to belong to, so removed findings decrement them).

Concurrency: transaction-scoped advisory locks per fingerprint, taken in
sorted order, serialise workers grouping into the same issue without
deadlocks; aggregates are recomputed under those locks.

Regression rule, for a *new* occurrence of a ``resolved`` issue whose trace
started after the resolution:
* if the issue was resolved with ``fixed_in_version`` and the occurrence comes
  from a version already affected before resolution (other than the fixed
  one), it is a straggler from an old deploy: the issue stays resolved and a
  ``straggler`` event is recorded once per version;
* otherwise the issue reopens as a regression.
Ignored issues never reopen. Replay traces are never grouped.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from rootsignal_server.issues.fingerprint import (
    FindingFacts,
    description_for,
    fingerprint,
    title_for,
    validate_overrides,
)

logger = logging.getLogger("rootsignal.issues")
SYSTEM_ACTOR = "system"


@dataclass(frozen=True)
class GroupingResult:
    issues_touched: int = 0
    issues_created: int = 0
    occurrences_added: int = 0
    regressions: int = 0


def issues_for_trace(conn: Connection, project_id: uuid.UUID, trace_id: str) -> set[uuid.UUID]:
    return set(conn.execute(text(
        "SELECT DISTINCT issue_id FROM issue_occurrences WHERE project_id = :p AND trace_id = :t"),
        {"p": project_id, "t": trace_id}).scalars())


def _load_overrides(conn: Connection, project_id: uuid.UUID) -> dict[str, tuple[str, ...]]:
    raw = conn.execute(text("SELECT issue_grouping FROM projects WHERE id = :p"),
                       {"p": project_id}).scalar_one()
    try:
        return validate_overrides(raw)
    except ValueError as exc:
        logger.error("invalid issue_grouping for project %s (%s); using defaults",
                     project_id, exc)
        return {}


def record_event(conn: Connection, *, org_id: uuid.UUID, project_id: uuid.UUID,
                 issue_id: uuid.UUID, kind: str, actor: str, from_status: str | None = None,
                 to_status: str | None = None, details: dict[str, Any] | None = None) -> None:
    conn.execute(text(
        "INSERT INTO issue_events (org_id, project_id, issue_id, kind, actor, from_status, "
        "to_status, details) VALUES (:o, :p, :i, :k, :a, :f, :t, CAST(:d AS jsonb))"),
        {"o": org_id, "p": project_id, "i": issue_id, "k": kind, "a": actor, "f": from_status,
         "t": to_status, "d": json.dumps(details or {})})


def group_trace(conn: Connection, project_id: uuid.UUID, trace_id: str, *,
                previous_issue_ids: Iterable[uuid.UUID] = ()) -> GroupingResult:
    head = conn.execute(text(
        "SELECT org_id, environment_id, started_at, source FROM traces "
        "WHERE project_id = :p AND trace_id = :t"), {"p": project_id, "t": trace_id}).one_or_none()
    if head is None or head.source == "replay":
        return GroupingResult()
    org_id, environment_id, occurred_at = head.org_id, head.environment_id, head.started_at

    findings = conn.execute(text(
        "SELECT id, detector, finding_key, severity, agent_name, agent_version, tool_name, "
        "error_type, cardinality(evidence_span_ids) AS weight FROM findings "
        "WHERE project_id = :p AND trace_id = :t"), {"p": project_id, "t": trace_id}).all()
    prior = set(previous_issue_ids) | issues_for_trace(conn, project_id, trace_id)
    overrides = _load_overrides(conn, project_id)
    planned = sorted(
        ((fingerprint(FindingFacts(f.detector, f.finding_key, f.agent_name, f.agent_version,
                                   f.tool_name, f.error_type), overrides), f) for f in findings),
        key=lambda pair: pair[0].value)

    prior_fps = set(conn.execute(text("SELECT fingerprint FROM issues WHERE id = ANY(:ids)"),
                                 {"ids": list(prior)}).scalars()) if prior else set()
    for fp in sorted({fp.value for fp, _ in planned} | prior_fps):
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"issue:{project_id}:{environment_id}:{fp}"})

    touched: set[uuid.UUID] = set()
    created = added = regressions = 0
    for fp, finding in planned:
        issue = conn.execute(text(
            "INSERT INTO issues (org_id, project_id, environment_id, fingerprint, components, "
            "detector, agent_name, tool_name, error_type, title, description, severity, "
            "first_seen_at, last_seen_at) VALUES (:o, :p, :e, :fp, CAST(:c AS jsonb), :d, "
            ":agent, :tool, :err, :title, :desc, :sev, :at, :at) "
            "ON CONFLICT (project_id, environment_id, fingerprint) DO UPDATE SET "
            "updated_at = now() "
            "RETURNING id, status, resolved_at, fixed_in_version, (xmax = 0) AS created"),
            {"o": org_id, "p": project_id, "e": environment_id, "fp": fp.value,
             "c": json.dumps(dict(fp.components)), "d": finding.detector,
             "agent": fp.components.get("agent_name", finding.agent_name),
             "tool": fp.components.get("tool_name", finding.tool_name),
             "err": fp.components.get("error_type", finding.error_type),
             "title": title_for(fp.components), "desc": description_for(finding.detector),
             "sev": finding.severity, "at": occurred_at}).one()
        touched.add(issue.id)
        if issue.created:
            created += 1
            record_event(conn, org_id=org_id, project_id=project_id, issue_id=issue.id,
                         kind="created", actor=SYSTEM_ACTOR, to_status="open",
                         details={"trace_id": trace_id, "components": dict(fp.components)})

        occurrence = conn.execute(text(
            "INSERT INTO issue_occurrences (org_id, project_id, environment_id, issue_id, "
            "finding_id, trace_id, agent_version, severity, weight, occurred_at) "
            "VALUES (:o, :p, :e, :i, :f, :t, :v, :sev, :w, :at) "
            "ON CONFLICT (finding_id) DO UPDATE SET issue_id = EXCLUDED.issue_id, "
            "agent_version = EXCLUDED.agent_version, severity = EXCLUDED.severity, "
            "weight = EXCLUDED.weight, occurred_at = EXCLUDED.occurred_at "
            "RETURNING (xmax = 0) AS inserted"),
            {"o": org_id, "p": project_id, "e": environment_id, "i": issue.id, "f": finding.id,
             "t": trace_id, "v": finding.agent_version, "sev": finding.severity,
             "w": max(finding.weight or 1, 1), "at": occurred_at}).one()
        if not occurrence.inserted:
            continue
        added += 1
        if _apply_regression_rule(conn, org_id, project_id, issue, finding.agent_version,
                                  trace_id, occurred_at):
            regressions += 1

    recompute(conn, touched | prior)
    return GroupingResult(len(touched | prior), created, added, regressions)


def _apply_regression_rule(conn: Connection, org_id: uuid.UUID, project_id: uuid.UUID,
                           issue: Any, agent_version: str | None, trace_id: str,
                           occurred_at: datetime) -> bool:
    current = conn.execute(text(
        "SELECT status, resolved_at, fixed_in_version FROM issues WHERE id = :i"),
        {"i": issue.id}).one()  # re-read: an earlier finding in this trace may have reopened it
    if current.status != "resolved" or current.resolved_at is None:
        return False
    if occurred_at <= current.resolved_at:
        return False  # an old run, re-detected: not evidence the fix failed
    if current.fixed_in_version and agent_version and agent_version != current.fixed_in_version:
        affected_before = conn.execute(text(
            "SELECT 1 FROM issue_occurrences WHERE issue_id = :i AND agent_version = :v "
            "AND occurred_at <= :resolved LIMIT 1"),
            {"i": issue.id, "v": agent_version, "resolved": current.resolved_at}).first()
        if affected_before is not None:
            already = conn.execute(text(
                "SELECT 1 FROM issue_events WHERE issue_id = :i AND kind = 'straggler' "
                "AND details ->> 'agent_version' = :v AND created_at > :resolved LIMIT 1"),
                {"i": issue.id, "v": agent_version, "resolved": current.resolved_at}).first()
            if already is None:
                record_event(conn, org_id=org_id, project_id=project_id, issue_id=issue.id,
                             kind="straggler", actor=SYSTEM_ACTOR,
                             details={"trace_id": trace_id, "agent_version": agent_version,
                                      "fixed_in_version": current.fixed_in_version})
            return False
    conn.execute(text(
        "UPDATE issues SET status = 'open', is_regression = true, "
        "regression_count = regression_count + 1, last_regressed_at = now(), "
        "resolved_at = NULL, fixed_in_version = NULL, updated_at = now() WHERE id = :i"),
        {"i": issue.id})
    record_event(conn, org_id=org_id, project_id=project_id, issue_id=issue.id,
                 kind="regressed", actor=SYSTEM_ACTOR, from_status="resolved", to_status="open",
                 details={"trace_id": trace_id, "agent_version": agent_version,
                          "fixed_in_version": current.fixed_in_version,
                          "resolved_at": current.resolved_at.isoformat()})
    return True


_SEVERITY_RANK = ("CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
                  "WHEN 'medium' THEN 2 ELSE 1 END")

_RECOMPUTE = text(f"""
WITH ids AS (SELECT unnest(:ids) AS id),
agg AS (
    SELECT issue_id, count(DISTINCT trace_id) AS traces, min(occurred_at) AS first_seen,
           max(occurred_at) AS last_seen, max({_SEVERITY_RANK}) AS rank,
           coalesce(array_agg(DISTINCT agent_version ORDER BY agent_version)
                    FILTER (WHERE agent_version IS NOT NULL), ARRAY[]::text[]) AS versions
    FROM issue_occurrences WHERE issue_id = ANY(:ids) GROUP BY issue_id
), first_trace AS (
    SELECT DISTINCT ON (issue_id) issue_id, trace_id FROM issue_occurrences
    WHERE issue_id = ANY(:ids) ORDER BY issue_id, occurred_at, id
), latest_trace AS (
    SELECT DISTINCT ON (issue_id) issue_id, trace_id FROM issue_occurrences
    WHERE issue_id = ANY(:ids) ORDER BY issue_id, occurred_at DESC, id
), worst_trace AS (
    SELECT DISTINCT ON (issue_id) issue_id, trace_id FROM issue_occurrences
    WHERE issue_id = ANY(:ids) ORDER BY issue_id, {_SEVERITY_RANK} DESC, weight DESC,
                                        occurred_at DESC, id
)
UPDATE issues i SET
    occurrence_count = coalesce(agg.traces, 0),
    first_seen_at = coalesce(agg.first_seen, i.first_seen_at),
    last_seen_at = coalesce(agg.last_seen, i.last_seen_at),
    affected_versions = coalesce(agg.versions, ARRAY[]::text[]),
    severity = CASE agg.rank WHEN 4 THEN 'critical' WHEN 3 THEN 'high' WHEN 2 THEN 'medium'
                             WHEN 1 THEN 'low' ELSE i.severity END,
    first_trace_id = first_trace.trace_id,
    latest_trace_id = latest_trace.trace_id,
    worst_trace_id = worst_trace.trace_id,
    updated_at = now()
FROM ids
LEFT JOIN agg ON agg.issue_id = ids.id
LEFT JOIN first_trace ON first_trace.issue_id = ids.id
LEFT JOIN latest_trace ON latest_trace.issue_id = ids.id
LEFT JOIN worst_trace ON worst_trace.issue_id = ids.id
WHERE i.id = ids.id
""").bindparams(bindparam("ids", type_=ARRAY(UUID(as_uuid=True))))


def recompute(conn: Connection, issue_ids: Iterable[uuid.UUID]) -> None:
    ids = sorted(set(issue_ids))
    if ids:
        conn.execute(_RECOMPUTE, {"ids": ids})


def regroup_project(conn: Connection, project_id: uuid.UUID) -> int:
    """Re-group every trace that has findings (after a grouping config change or backfill)."""
    trace_ids = conn.execute(text(
        "SELECT DISTINCT trace_id FROM findings WHERE project_id = :p ORDER BY trace_id"),
        {"p": project_id}).scalars().all()
    for trace_id in trace_ids:
        group_trace(conn, project_id, trace_id)
    return len(trace_ids)

