"""Human status changes on issues (resolve, ignore, reopen), recorded in history."""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import Connection, text

from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.issues.grouping import record_event

IssueStatus = Literal["open", "resolved", "ignored"]


class InvalidTransition(ValueError):
    pass


def change_status(conn: Connection, tenant: TenantContext, issue_id: uuid.UUID,
                  status: IssueStatus, *, actor: str, fixed_in_version: str | None = None,
                  note: str | None = None) -> bool:
    """Apply a status change. Returns False if the issue isn't visible to ``tenant``."""
    if fixed_in_version is not None and status != "resolved":
        raise InvalidTransition("fixed_in_version only applies when resolving")
    current = conn.execute(text(
        "SELECT status FROM issues WHERE id = :i AND project_id = :p AND environment_id = :e "
        "FOR UPDATE"),
        {"i": issue_id, "p": tenant.project_id, "e": tenant.environment_id}).scalar_one_or_none()
    if current is None:
        return False
    if current == status:
        raise InvalidTransition(f"issue is already {status}")
    conn.execute(text(
        "UPDATE issues SET status = :s, updated_at = now(), "
        "resolved_at = CASE WHEN :s = 'resolved' THEN now() END, "
        "fixed_in_version = CASE WHEN :s = 'resolved' THEN CAST(:fixed AS text) END, "
        # A deliberate reopen or resolve ends the 'regression' state.
        "is_regression = CASE WHEN :s = 'ignored' THEN is_regression ELSE false END "
        "WHERE id = :i"), {"s": status, "fixed": fixed_in_version, "i": issue_id})
    details: dict[str, str] = {}
    if fixed_in_version:
        details["fixed_in_version"] = fixed_in_version
    if note:
        details["note"] = note
    record_event(conn, org_id=tenant.org_id, project_id=tenant.project_id, issue_id=issue_id,
                 kind="status_changed", actor=actor, from_status=current, to_status=status,
                 details=details)
    return True
