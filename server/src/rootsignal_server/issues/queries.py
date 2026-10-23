"""Tenant-scoped issue reads. Every query filters by project and environment."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Connection, text

from rootsignal_server.auth.keys import TenantContext

IssueSort = Literal["last_seen", "occurrences", "severity"]

_RANK = ("CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
         "WHEN 'medium' THEN 2 ELSE 1 END")
ISSUE_COLUMNS = (
    "id, fingerprint, components, detector, agent_name, tool_name, error_type, title, "
    "description, severity, status, is_regression, regression_count, last_regressed_at, "
    "first_seen_at, last_seen_at, occurrence_count, affected_versions, first_trace_id, "
    "latest_trace_id, worst_trace_id, resolved_at, fixed_in_version, created_at, updated_at"
)


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class _SortSpec:
    columns: tuple[str, ...]  # SQL expressions, all ordered DESC; the last one is unique
    parsers: tuple[Callable[[Any], Any], ...]


_SORTS: dict[str, _SortSpec] = {
    "last_seen": _SortSpec(("last_seen_at", "id"), (datetime.fromisoformat, uuid.UUID)),
    "occurrences": _SortSpec(("occurrence_count", "id"), (int, uuid.UUID)),
    "severity": _SortSpec((f"({_RANK})", "last_seen_at", "id"),
                          (int, datetime.fromisoformat, uuid.UUID)),
}


@dataclass(frozen=True)
class IssueFilters:
    status: str | None = None
    severity: str | None = None
    detector: str | None = None
    agent_name: str | None = None
    regression: bool | None = None


def _cursor_values(row: dict[str, Any], sort: str) -> list[Any]:
    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    values = {"last_seen": [row["last_seen_at"], row["id"]],
              "occurrences": [row["occurrence_count"], row["id"]],
              "severity": [rank[row["severity"]], row["last_seen_at"], row["id"]]}[sort]
    return [v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, uuid.UUID)
            else v for v in values]


def _decode_cursor(cursor: str, spec: _SortSpec) -> list[Any]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(raw, list) or len(raw) != len(spec.parsers):
            raise ValueError("wrong shape")
        return [parse(v) for parse, v in zip(spec.parsers, raw, strict=True)]
    except (ValueError, TypeError, binascii.Error) as exc:
        raise InvalidCursor("invalid cursor") from exc


def list_issues(conn: Connection, tenant: TenantContext, filters: IssueFilters, *,
                sort: IssueSort, limit: int, cursor: str | None,
                ) -> tuple[list[dict[str, Any]], str | None]:
    spec = _SORTS[sort]
    clauses = ["project_id = :p", "environment_id = :e"]
    params: dict[str, Any] = {"p": tenant.project_id, "e": tenant.environment_id,
                              "limit": limit + 1}
    for column, value in (("status", filters.status), ("severity", filters.severity),
                          ("detector", filters.detector), ("agent_name", filters.agent_name),
                          ("is_regression", filters.regression)):
        if value is not None:
            clauses.append(f"{column} = :{column}")
            params[column] = value
    if cursor:
        values = _decode_cursor(cursor, spec)
        names = [f"c{i}" for i in range(len(values))]
        params.update(zip(names, values, strict=True))
        clauses.append(f"({', '.join(spec.columns)}) < ({', '.join(':' + n for n in names)})")
    order = ", ".join(f"{c} DESC" for c in spec.columns)
    rows = conn.execute(text(
        f"SELECT {ISSUE_COLUMNS} FROM issues WHERE {' AND '.join(clauses)} "
        f"ORDER BY {order} LIMIT :limit"), params).mappings().all()
    items = [dict(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        raw = json.dumps(_cursor_values(items[-1], sort)).encode()
        next_cursor = base64.urlsafe_b64encode(raw).decode()
    return items, next_cursor


def get_issue(conn: Connection, tenant: TenantContext,
              issue_id: uuid.UUID) -> dict[str, Any] | None:
    issue = conn.execute(text(
        f"SELECT {ISSUE_COLUMNS} FROM issues "
        "WHERE id = :i AND project_id = :p AND environment_id = :e"),
        {"i": issue_id, "p": tenant.project_id, "e": tenant.environment_id}
    ).mappings().one_or_none()
    if issue is None:
        return None
    versions = conn.execute(text(
        "SELECT agent_version, count(DISTINCT trace_id) AS occurrences, "
        "min(occurred_at) AS first_seen_at, max(occurred_at) AS last_seen_at "
        "FROM issue_occurrences WHERE issue_id = :i GROUP BY agent_version "
        "ORDER BY max(occurred_at) DESC"), {"i": issue_id}).mappings().all()
    events = conn.execute(text(
        "SELECT kind, actor, from_status, to_status, details, created_at FROM issue_events "
        "WHERE issue_id = :i ORDER BY created_at, id"), {"i": issue_id}).mappings().all()
    recent, _ = list_occurrences(conn, tenant, issue_id, limit=20, cursor=None)
    return {"issue": dict(issue), "versions": [dict(v) for v in versions],
            "events": [dict(e) for e in events], "recent_occurrences": recent}


def list_occurrences(conn: Connection, tenant: TenantContext, issue_id: uuid.UUID, *,
                     limit: int, cursor: str | None,
                     ) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {"i": issue_id, "p": tenant.project_id,
                              "e": tenant.environment_id, "limit": limit + 1}
    after = ""
    if cursor:
        spec = _SortSpec(("occurred_at", "id"), (datetime.fromisoformat, uuid.UUID))
        params["c0"], params["c1"] = _decode_cursor(cursor, spec)
        after = "AND (o.occurred_at, o.id) < (:c0, :c1) "
    rows = conn.execute(text(
        "SELECT o.id, o.trace_id, o.finding_id, o.agent_version, o.severity, o.occurred_at, "
        "f.title AS finding_title, f.summary AS finding_summary "
        "FROM issue_occurrences o JOIN findings f ON f.id = o.finding_id "
        "WHERE o.issue_id = :i AND o.project_id = :p AND o.environment_id = :e " + after +
        "ORDER BY o.occurred_at DESC, o.id DESC LIMIT :limit"), params).mappings().all()
    items = [dict(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        raw = json.dumps([items[-1]["occurred_at"].isoformat(), str(items[-1]["id"])]).encode()
        next_cursor = base64.urlsafe_b64encode(raw).decode()
    return items, next_cursor


def issue_exists(conn: Connection, tenant: TenantContext, issue_id: uuid.UUID) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM issues WHERE id = :i AND project_id = :p AND environment_id = :e"),
        {"i": issue_id, "p": tenant.project_id, "e": tenant.environment_id}).first() is not None
