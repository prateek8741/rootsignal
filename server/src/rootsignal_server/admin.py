"""Tenant and API-key administration. Used by the CLI; every change is audit-logged."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.auth.keys import generate_key
from rootsignal_server.storage.models import API_KEY_SCOPES


class AdminError(ValueError):
    pass


@dataclass(frozen=True)
class TenantIds:
    org_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID


@dataclass(frozen=True)
class CreatedKey:
    key_id: uuid.UUID
    prefix: str
    plaintext: str  # shown exactly once; never stored


def audit(conn: Connection, *, actor: str, action: str, org_id: uuid.UUID | None,
          project_id: uuid.UUID | None = None, target: str | None = None,
          details: dict[str, Any] | None = None) -> None:
    conn.execute(text(
        "INSERT INTO audit_events (org_id, project_id, actor, action, target, details) "
        "VALUES (:o, :p, :a, :act, :t, CAST(:d AS jsonb))"),
        {"o": org_id, "p": project_id, "a": actor, "act": action, "t": target,
         "d": json.dumps(details or {})})


def ensure_tenant(conn: Connection, *, org_slug: str, project_slug: str, environment: str,
                  actor: str, org_name: str | None = None,
                  project_name: str | None = None) -> TenantIds:
    """Get or create an org, project and environment (idempotent)."""
    org_id = conn.execute(text(
        "INSERT INTO organizations (slug, name) VALUES (:s, :n) "
        "ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id"),
        {"s": org_slug, "n": org_name or org_slug}).scalar_one()
    project_id = conn.execute(text(
        "INSERT INTO projects (org_id, slug, name) VALUES (:o, :s, :n) "
        "ON CONFLICT (org_id, slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id"),
        {"o": org_id, "s": project_slug, "n": project_name or project_slug}).scalar_one()
    environment_id = conn.execute(text(
        "INSERT INTO environments (org_id, project_id, name) VALUES (:o, :p, :n) "
        "ON CONFLICT (project_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id"),
        {"o": org_id, "p": project_id, "n": environment}).scalar_one()
    audit(conn, actor=actor, action="tenant.ensure", org_id=org_id, project_id=project_id,
          target=f"{org_slug}/{project_slug}/{environment}")
    return TenantIds(org_id, project_id, environment_id)


def find_tenant(conn: Connection, org_slug: str, project_slug: str,
                environment: str) -> TenantIds:
    row = conn.execute(text(
        "SELECT o.id AS org_id, p.id AS project_id, e.id AS environment_id "
        "FROM organizations o JOIN projects p ON p.org_id = o.id "
        "JOIN environments e ON e.project_id = p.id "
        "WHERE o.slug = :o AND p.slug = :p AND e.name = :e"),
        {"o": org_slug, "p": project_slug, "e": environment}).one_or_none()
    if row is None:
        raise AdminError(f"no environment {org_slug}/{project_slug}/{environment}")
    return TenantIds(row.org_id, row.project_id, row.environment_id)


def create_api_key(conn: Connection, tenant: TenantIds, *, name: str, scopes: Iterable[str],
                   actor: str) -> CreatedKey:
    scope_list = sorted(set(scopes))
    unknown = set(scope_list) - set(API_KEY_SCOPES)
    if not scope_list or unknown:
        raise AdminError(f"scopes must be a non-empty subset of {API_KEY_SCOPES}")
    key = generate_key()
    key_id = conn.execute(text(
        "INSERT INTO api_keys (org_id, project_id, environment_id, name, prefix, key_hash, "
        "scopes) VALUES (:o, :p, :e, :n, :prefix, :h, :scopes) RETURNING id"),
        {"o": tenant.org_id, "p": tenant.project_id, "e": tenant.environment_id, "n": name,
         "prefix": key.prefix, "h": key.key_hash, "scopes": scope_list}).scalar_one()
    audit(conn, actor=actor, action="api_key.create", org_id=tenant.org_id,
          project_id=tenant.project_id, target=key.prefix,
          details={"name": name, "scopes": scope_list})
    return CreatedKey(key_id, key.prefix, key.plaintext)


def revoke_api_key(conn: Connection, prefix: str, *, actor: str) -> None:
    row = conn.execute(text(
        "UPDATE api_keys SET revoked_at = now() WHERE prefix = :prefix AND revoked_at IS NULL "
        "RETURNING org_id, project_id"), {"prefix": prefix}).one_or_none()
    if row is None:
        raise AdminError(f"no active key with prefix {prefix!r}")
    audit(conn, actor=actor, action="api_key.revoke", org_id=row.org_id,
          project_id=row.project_id, target=prefix)
