"""API keys: generation, hashing, authentication.

A key is ``rs_`` + 43 URL-safe characters (256 bits of randomness). Only its
SHA-256 is stored. A slow hash would add nothing for keys with this much
entropy, and a fast one lets every request authenticate with a single
indexed lookup.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

KEY_PREFIX = "rs_"
DISPLAY_PREFIX_LEN = 11  # "rs_" + 8 characters, safe to show in listings
_LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True)
class TenantContext:
    """Who a request acts for. Every data access takes one of these."""

    org_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID
    key_id: uuid.UUID
    scopes: frozenset[str]
    key_prefix: str = ""  # display prefix, for audit actors; never the secret

    @property
    def actor(self) -> str:
        return f"api_key:{self.key_prefix}" if self.key_prefix else "api_key"

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class GeneratedKey:
    plaintext: str
    prefix: str
    key_hash: str


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_key() -> GeneratedKey:
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return GeneratedKey(plaintext, plaintext[:DISPLAY_PREFIX_LEN], hash_key(plaintext))


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(KEY_PREFIX) or len(token) > 128:
        return None
    return token


def authenticate(conn: Connection, plaintext: str) -> TenantContext | None:
    """Resolve a key to its tenant, or ``None`` if unknown or revoked."""
    row = conn.execute(
        text("SELECT id, org_id, project_id, environment_id, scopes, last_used_at, prefix "
             "FROM api_keys WHERE key_hash = :h AND revoked_at IS NULL"),
        {"h": hash_key(plaintext)},
    ).one_or_none()
    if row is None:
        return None
    now = datetime.now(UTC)
    if row.last_used_at is None or now - row.last_used_at > _LAST_USED_RESOLUTION:
        # Coarse-grained so authentication doesn't cost a write per request.
        conn.execute(text("UPDATE api_keys SET last_used_at = :now WHERE id = :id"),
                     {"now": now, "id": row.id})
    return TenantContext(row.org_id, row.project_id, row.environment_id, row.id,
                         frozenset(row.scopes), row.prefix)
