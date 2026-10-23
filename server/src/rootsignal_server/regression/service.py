"""Regression case lifecycle: create from an issue, edit as new versions, read."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.regression.builder import CaseSourceError, assess, build_spec
from rootsignal_server.regression.spec import CaseInput, CaseSpec

UNSET: Any = object()


@dataclass(frozen=True)
class CaseEdit:
    name: str | None = None
    entrypoint: Any = UNSET  # str | None
    input: Any = UNSET  # any JSON value; dict = keyword arguments
    disabled_assertions: list[str] | None = None
    note: str | None = None


def create_case(conn: Connection, tenant: TenantContext, issue_id: uuid.UUID, *,
                trace_id: str | None = None, name: str | None = None,
                input_override: Any = UNSET) -> uuid.UUID | None:
    """Create version 1 from an occurrence (default: the issue's worst trace).
    Returns ``None`` if the issue isn't visible; raises ``CaseSourceError`` on a bad trace."""
    issue = conn.execute(text(
        "SELECT id, title, worst_trace_id FROM issues "
        "WHERE id = :i AND project_id = :p AND environment_id = :e"),
        {"i": issue_id, "p": tenant.project_id, "e": tenant.environment_id}).one_or_none()
    if issue is None:
        return None
    source = trace_id or issue.worst_trace_id
    if source is None:
        raise CaseSourceError("the issue has no occurrences to build a case from")
    case_name = name or f"{issue.title} (trace {source[:8]})"
    spec = build_spec(conn, project_id=tenant.project_id, environment_id=tenant.environment_id,
                      issue_id=issue_id, trace_id=source, name=case_name,
                      input_override=None if input_override is UNSET else input_override,
                      has_override=input_override is not UNSET)
    case_id: uuid.UUID = conn.execute(text(
        "INSERT INTO regression_cases (org_id, project_id, environment_id, issue_id, "
        "source_trace_id, name, created_by) VALUES (:o, :p, :e, :i, :t, :n, :by) RETURNING id"),
        {"o": tenant.org_id, "p": tenant.project_id, "e": tenant.environment_id,
         "i": issue_id, "t": source, "n": case_name, "by": tenant.actor}).scalar_one()
    _insert_version(conn, tenant, case_id, 1, spec, f"created from trace {source[:8]}")
    return case_id


def _insert_version(conn: Connection, tenant: TenantContext, case_id: uuid.UUID, version: int,
                    spec: CaseSpec, note: str | None) -> None:
    conn.execute(text(
        "INSERT INTO regression_case_versions (case_id, version, project_id, spec, change_note, "
        "created_by) VALUES (:c, :v, :p, CAST(:spec AS jsonb), :note, :by)"),
        {"c": case_id, "v": version, "p": tenant.project_id, "spec": spec.model_dump_json(),
         "note": note, "by": tenant.actor})


class InvalidEdit(ValueError):
    pass


def edit_case(conn: Connection, tenant: TenantContext, case_id: uuid.UUID,
              edit: CaseEdit) -> bool:
    """Apply an edit as a new version. False if the case isn't visible."""
    row = conn.execute(text(
        "SELECT c.current_version, v.spec FROM regression_cases c "
        "JOIN regression_case_versions v ON v.case_id = c.id AND v.version = c.current_version "
        "WHERE c.id = :c AND c.project_id = :p AND c.environment_id = :e FOR UPDATE OF c"),
        {"c": case_id, "p": tenant.project_id, "e": tenant.environment_id}).one_or_none()
    if row is None:
        return False
    spec = CaseSpec.model_validate(row.spec)
    changes: dict[str, Any] = {}
    if edit.name is not None:
        changes["name"] = edit.name
    if edit.entrypoint is not UNSET:
        changes["entrypoint"] = edit.entrypoint
    if edit.input is not UNSET:
        changes["input"] = CaseInput(kind="kwargs" if isinstance(edit.input, dict) else "value",
                                     value=edit.input, supplied_by_user=True)
    if edit.disabled_assertions is not None:
        known = {a.id for a in spec.assertions}
        unknown = set(edit.disabled_assertions) - known
        if unknown:
            raise InvalidEdit(f"unknown assertion id(s): {sorted(unknown)}")
        changes["assertions"] = [a.model_copy(update={"enabled": a.id not in
                                                      edit.disabled_assertions})
                                 for a in spec.assertions]
    if not changes:
        raise InvalidEdit("nothing to change")
    updated = spec.model_copy(update=changes)
    updated = updated.model_copy(update={"quality": assess(updated)})
    version = row.current_version + 1
    _insert_version(conn, tenant, case_id, version, updated, edit.note)
    conn.execute(text(
        "UPDATE regression_cases SET current_version = :v, name = :n, updated_at = now() "
        "WHERE id = :c"), {"v": version, "n": updated.name, "c": case_id})
    return True


# --- reads -------------------------------------------------------------------------

_CASE_COLUMNS = ("c.id, c.issue_id, c.source_trace_id, c.name, c.status, c.current_version, "
                 "c.created_by, c.created_at, c.updated_at")


def _summary(row: Any) -> dict[str, Any]:
    spec = row["spec"]
    data = {k: row[k] for k in ("id", "issue_id", "source_trace_id", "name", "status",
                                "current_version", "created_by", "created_at", "updated_at")}
    return {**data, "entrypoint": spec["entrypoint"],
            "replayable_recorded": spec["quality"]["replayable_recorded"],
            "assertion_count": sum(1 for a in spec["assertions"] if a["enabled"]),
            "warning_count": len(spec["quality"]["warnings"])}


def list_cases(conn: Connection, tenant: TenantContext, *, issue_id: uuid.UUID | None = None,
               status: str | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        f"SELECT {_CASE_COLUMNS}, v.spec FROM regression_cases c "
        "JOIN regression_case_versions v ON v.case_id = c.id AND v.version = c.current_version "
        "WHERE c.project_id = :p AND c.environment_id = :e "
        "AND (CAST(:i AS uuid) IS NULL OR c.issue_id = :i) "
        "AND (CAST(:s AS text) IS NULL OR c.status = :s) "
        "ORDER BY c.created_at DESC LIMIT 200"),
        {"p": tenant.project_id, "e": tenant.environment_id, "i": issue_id,
         "s": status}).mappings().all()
    return [_summary(r) for r in rows]


def get_case(conn: Connection, tenant: TenantContext, case_id: uuid.UUID,
             version: int | None = None) -> dict[str, Any] | None:
    row = conn.execute(text(
        f"SELECT {_CASE_COLUMNS}, v.spec, v.version FROM regression_cases c "
        "JOIN regression_case_versions v ON v.case_id = c.id "
        "AND v.version = coalesce(CAST(:v AS integer), c.current_version) "
        "WHERE c.id = :c AND c.project_id = :p AND c.environment_id = :e"),
        {"c": case_id, "p": tenant.project_id, "e": tenant.environment_id,
         "v": version}).mappings().one_or_none()
    if row is None:
        return None
    versions = conn.execute(text(
        "SELECT version, created_by, created_at, change_note FROM regression_case_versions "
        "WHERE case_id = :c ORDER BY version DESC"), {"c": case_id}).mappings().all()
    return {**_summary(row), "version": row["version"], "spec": row["spec"],
            "versions": [dict(v) for v in versions]}


def spec_document(case: dict[str, Any]) -> dict[str, Any]:
    """The spec as a standalone document (for the replay runner and CI files)."""
    return {"case_id": str(case["id"]), "version": case["version"], **case["spec"]}


__all__ = ["UNSET", "CaseEdit", "CaseSourceError", "InvalidEdit", "create_case", "edit_case",
           "get_case", "list_cases", "spec_document"]
