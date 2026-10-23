"""Dashboard sessions and one-time login links.

The browser never holds an API key. It gets a random session token in an
``HttpOnly; SameSite=Strict`` cookie; we store only the token's SHA-256.
Sessions are limited to ``read`` and ``triage`` (never ``ingest``/``admin``),
expire after an absolute lifetime or an idle period, end on logout, and end
when the API key they were created from is revoked.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Connection, text

from rootsignal_server.admin import TenantIds, audit
from rootsignal_server.auth.keys import TenantContext

SESSION_COOKIE = "rs_session"
CSRF_HEADER = "x-rootsignal-csrf"
SESSION_SCOPES = frozenset({"read", "triage"})
_LAST_SEEN_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True)
class SessionPolicy:
    absolute_ttl: timedelta = timedelta(hours=12)
    idle_timeout: timedelta = timedelta(hours=2)


@dataclass(frozen=True)
class NewSession:
    token: str  # goes into the cookie, never stored
    session_id: uuid.UUID
    expires_at: datetime


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _session_label(session_id: uuid.UUID) -> str:
    return f"s_{session_id.hex[:8]}"


def _insert_session(conn: Connection, *, org_id: uuid.UUID, project_id: uuid.UUID,
                    environment_id: uuid.UUID, scopes: list[str], api_key_id: uuid.UUID | None,
                    created_via: Literal["login_link", "api_key"], actor: str,
                    policy: SessionPolicy) -> NewSession:
    token = _new_token()
    expires_at = datetime.now(UTC) + policy.absolute_ttl
    session_id = uuid.uuid4()
    label = _session_label(session_id)
    conn.execute(text(
        "INSERT INTO dashboard_sessions (id, token_hash, org_id, project_id, environment_id, "
        "scopes, api_key_id, created_via, label, expires_at) VALUES (:id, :h, :o, :p, :e, "
        ":scopes, :key, :via, :label, :exp)"),
        {"id": session_id, "h": _hash(token), "o": org_id, "p": project_id, "e": environment_id,
         "scopes": sorted(scopes), "key": api_key_id, "via": created_via, "label": label,
         "exp": expires_at})
    audit(conn, actor=actor, action="dashboard.session_created", org_id=org_id,
          project_id=project_id, target=label,
          details={"via": created_via, "scopes": sorted(scopes)})
    return NewSession(token, session_id, expires_at)


def create_login_link(conn: Connection, tenant: TenantIds, *, scopes: list[str],
                      created_by: str, ttl: timedelta = timedelta(minutes=5)) -> str:
    """Return a single-use token; the caller builds the URL (token in the fragment)."""
    chosen = sorted(set(scopes))
    if not chosen or not set(chosen) <= SESSION_SCOPES:
        allowed = sorted(SESSION_SCOPES)
        raise ValueError(f"login link scopes must be a non-empty subset of {allowed}")
    if "read" not in chosen:
        raise ValueError("a dashboard session needs the 'read' scope")
    token = _new_token()
    conn.execute(text(
        "INSERT INTO login_links (token_hash, org_id, project_id, environment_id, scopes, "
        "created_by, expires_at) VALUES (:h, :o, :p, :e, :scopes, :by, now() + :ttl)"),
        {"h": _hash(token), "o": tenant.org_id, "p": tenant.project_id,
         "e": tenant.environment_id, "scopes": chosen, "by": created_by, "ttl": ttl})
    audit(conn, actor=created_by, action="dashboard.login_link_created", org_id=tenant.org_id,
          project_id=tenant.project_id, details={"scopes": chosen, "ttl_s": ttl.total_seconds()})
    return token


def redeem_login_link(conn: Connection, token: str, policy: SessionPolicy) -> NewSession | None:
    """Consume a link (atomically: a link can never be used twice) and open a session."""
    link = conn.execute(text(
        "UPDATE login_links SET used_at = now() WHERE token_hash = :h AND used_at IS NULL "
        "AND expires_at > now() RETURNING org_id, project_id, environment_id, scopes, created_by"),
        {"h": _hash(token)}).one_or_none()
    if link is None:
        return None
    return _insert_session(conn, org_id=link.org_id, project_id=link.project_id,
                           environment_id=link.environment_id, scopes=list(link.scopes),
                           api_key_id=None, created_via="login_link",
                           actor=f"login_link:{link.created_by}", policy=policy)


def session_from_key(conn: Connection, key: TenantContext,
                     policy: SessionPolicy) -> NewSession | None:
    """Exchange an authenticated API key for a session (scopes ∩ {read, triage})."""
    scopes = sorted(key.scopes & SESSION_SCOPES)
    if "read" not in scopes:
        return None
    return _insert_session(conn, org_id=key.org_id, project_id=key.project_id,
                           environment_id=key.environment_id, scopes=scopes,
                           api_key_id=key.key_id, created_via="api_key", actor=key.actor,
                           policy=policy)


def authenticate_session(conn: Connection, token: str,
                         policy: SessionPolicy) -> TenantContext | None:
    row = conn.execute(text(
        "SELECT s.id, s.org_id, s.project_id, s.environment_id, s.scopes, s.label, "
        "s.last_seen_at FROM dashboard_sessions s "
        "LEFT JOIN api_keys k ON k.id = s.api_key_id "
        "WHERE s.token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > now() "
        "AND s.last_seen_at > now() - :idle "
        "AND (s.api_key_id IS NULL OR k.revoked_at IS NULL)"),
        {"h": _hash(token), "idle": policy.idle_timeout}).one_or_none()
    if row is None:
        return None
    if datetime.now(UTC) - row.last_seen_at > _LAST_SEEN_RESOLUTION:
        conn.execute(text("UPDATE dashboard_sessions SET last_seen_at = now() WHERE id = :id"),
                     {"id": row.id})
    return TenantContext(row.org_id, row.project_id, row.environment_id, row.id,
                         frozenset(row.scopes) & SESSION_SCOPES, row.label, principal="session")


def revoke_session(conn: Connection, token: str) -> bool:
    row = conn.execute(text(
        "UPDATE dashboard_sessions SET revoked_at = now() WHERE token_hash = :h "
        "AND revoked_at IS NULL RETURNING org_id, project_id, label"),
        {"h": _hash(token)}).one_or_none()
    if row is None:
        return False
    audit(conn, actor=f"session:{row.label}", action="dashboard.session_revoked",
          org_id=row.org_id, project_id=row.project_id, target=row.label)
    return True


def revoke_sessions(conn: Connection, tenant: TenantIds, *, actor: str,
                    whole_project: bool = False) -> int:
    scope_sql = "project_id = :p" + ("" if whole_project else " AND environment_id = :e")
    revoked = conn.execute(text(
        f"UPDATE dashboard_sessions SET revoked_at = now() WHERE {scope_sql} "
        "AND revoked_at IS NULL"),
        {"p": tenant.project_id, "e": tenant.environment_id}).rowcount
    audit(conn, actor=actor, action="dashboard.sessions_revoked", org_id=tenant.org_id,
          project_id=tenant.project_id, details={"count": revoked, "whole_project": whole_project})
    return revoked
