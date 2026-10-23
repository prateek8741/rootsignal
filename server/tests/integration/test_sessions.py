from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from conftest import TEST_DATABASE_URL, Harness, make_harness
from otlp_factory import raw_request
from rootsignal_server import admin
from rootsignal_server.auth.sessions import create_login_link
from rootsignal_server.cli import main as cli_main
from rootsignal_server.config import get_settings

CSRF = {"x-rootsignal-csrf": "1"}


def link_for(h: Harness, tenant: admin.TenantIds, scopes: tuple[str, ...] = ("read", "triage"),
             ttl: timedelta = timedelta(minutes=5)) -> str:
    with h.engine.begin() as conn:
        return create_login_link(conn, tenant, scopes=list(scopes), created_by="test", ttl=ttl)


def sign_in_with_link(h: Harness, token: str, headers: dict[str, str] | None = None) -> Any:
    return h.client.post("/v1/auth/link", json={"token": token},
                         headers=CSRF if headers is None else headers)


def test_login_link_sets_a_locked_down_cookie_and_works_once(harness: Harness) -> None:
    tenant = harness.tenant()
    token = link_for(harness, tenant)
    response = sign_in_with_link(harness, token)
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    for attribute in ("rs_session=", "HttpOnly", "SameSite=strict", "Path=/v1", "Max-Age="):
        assert attribute.lower() in cookie.lower()
    assert "secure" not in cookie.lower()  # local http default; must be enabled behind HTTPS
    context = response.json()
    assert context["principal"].startswith("session:s_")
    assert context["scopes"] == ["read", "triage"]

    assert harness.client.get("/v1/traces").status_code == 200  # cookie, no key anywhere
    harness.client.cookies.clear()
    assert sign_in_with_link(harness, token).status_code == 401  # single use


def test_expired_link_and_missing_csrf_header_are_refused(harness: Harness) -> None:
    tenant = harness.tenant()
    expired = link_for(harness, tenant, ttl=timedelta(seconds=-1))
    assert sign_in_with_link(harness, expired).status_code == 401
    fresh = link_for(harness, tenant)
    assert sign_in_with_link(harness, fresh, headers={}).status_code == 403  # login CSRF
    assert sign_in_with_link(harness, fresh).status_code == 200  # still unused, works


def test_only_hashes_are_stored(harness: Harness) -> None:
    tenant = harness.tenant()
    token = link_for(harness, tenant)
    sign_in_with_link(harness, token)
    session_token = harness.client.cookies.get("rs_session")
    assert session_token
    with harness.engine.connect() as conn:
        stored = " ".join(conn.execute(text(
            "SELECT token_hash FROM login_links UNION ALL SELECT token_hash FROM dashboard_sessions"
        )).scalars())
    assert token not in stored and session_token not in stored


def test_key_exchange_limits_scopes_and_needs_read(harness: Harness) -> None:
    tenant = harness.tenant()
    ingest_only = harness.key(tenant, "ingest")
    refused = harness.client.post("/v1/auth/session", json={"api_key": ingest_only}, headers=CSRF)
    assert refused.status_code == 403
    everything = harness.key(tenant, "ingest", "read", "triage")
    signed_in = harness.client.post("/v1/auth/session", json={"api_key": everything},
                                    headers=CSRF)
    assert signed_in.status_code == 200
    assert signed_in.json()["scopes"] == ["read", "triage"]  # never ingest/admin
    body = raw_request([{"span_id": b"\x01" * 8}]).SerializeToString()
    ingest = harness.client.post("/v1/traces", content=body,
                                 headers={"content-type": "application/x-protobuf", **CSRF})
    assert ingest.status_code == 403
    assert harness.client.post("/v1/auth/session", json={"api_key": "rs_" + "x" * 43},
                               headers=CSRF).status_code == 401


def test_state_changes_through_a_cookie_need_the_csrf_header(harness: Harness) -> None:
    tenant = harness.tenant()
    with harness.engine.begin() as conn:
        issue_id = conn.execute(text(
            "INSERT INTO issues (org_id, project_id, environment_id, fingerprint, components, "
            "detector, title, description, severity, first_seen_at, last_seen_at) "
            "VALUES (:o, :p, :e, 'fp', '{}', 'tool_error', 't', 'd', 'medium', now(), now()) "
            "RETURNING id"),
            {"p": tenant.project_id, "o": tenant.org_id, "e": tenant.environment_id}).scalar_one()
    sign_in_with_link(harness, link_for(harness, tenant))
    url = f"/v1/issues/{issue_id}/status"
    assert harness.client.post(url, json={"status": "ignored"}).status_code == 403
    changed = harness.client.post(url, json={"status": "ignored"}, headers=CSRF)
    assert changed.status_code == 200
    with harness.engine.connect() as conn:
        actor = conn.execute(text("SELECT actor FROM issue_events WHERE kind = 'status_changed'"
                                  )).scalar_one()
    assert actor.startswith("session:s_")


def test_revoking_the_key_ends_its_sessions(harness: Harness) -> None:
    tenant = harness.tenant()
    key = harness.key(tenant, "read")
    harness.client.post("/v1/auth/session", json={"api_key": key}, headers=CSRF)
    assert harness.client.get("/v1/traces").status_code == 200
    with harness.engine.begin() as conn:
        admin.revoke_api_key(conn, key[:11], actor="test")
    assert harness.client.get("/v1/traces").status_code == 401


def test_logout_ends_the_session_server_side(harness: Harness) -> None:
    tenant = harness.tenant()
    sign_in_with_link(harness, link_for(harness, tenant))
    token = harness.client.cookies.get("rs_session")
    assert harness.client.delete("/v1/auth/session").status_code == 403  # needs CSRF header
    logout = harness.client.delete("/v1/auth/session", headers=CSRF)
    assert logout.status_code == 204
    assert 'rs_session=""' in logout.headers["set-cookie"] or "Max-Age=0" in logout.headers[
        "set-cookie"]
    # Replaying the old cookie (e.g. copied before logout) no longer works.
    harness.client.cookies.set("rs_session", token or "")
    assert harness.client.get("/v1/traces").status_code == 401


@pytest.mark.parametrize("column", ["expires_at", "last_seen_at"])
def test_absolute_and_idle_expiry(harness: Harness, column: str) -> None:
    tenant = harness.tenant()
    sign_in_with_link(harness, link_for(harness, tenant))
    with harness.engine.begin() as conn:
        conn.execute(text(f"UPDATE dashboard_sessions SET {column} = now() - interval '13 hours'"))
    assert harness.client.get("/v1/traces").status_code == 401


def test_sessions_are_tenant_scoped(harness: Harness) -> None:
    a, b = harness.tenant("acme"), harness.tenant("globex")
    harness.post(raw_request([{"span_id": b"\x01" * 8}]), harness.key(a))
    trace_id = harness.scalar("SELECT trace_id FROM traces")
    sign_in_with_link(harness, link_for(harness, b))
    assert harness.client.get(f"/v1/traces/{trace_id}").status_code == 404


def test_login_attempts_are_rate_limited(clean_db: Engine) -> None:
    h = make_harness(clean_db, login_rate_limit_per_minute=2)
    statuses = [h.client.post("/v1/auth/link", json={"token": "x" * 43}, headers=CSRF).status_code
                for _ in range(3)]
    assert statuses == [401, 401, 429]


def test_cli_dashboard_link_and_revoke(harness: Harness, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    harness.tenant("acme", "support", "development")
    monkeypatch.setenv("ROOTSIGNAL_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    try:
        assert cli_main(["dashboard-link", "--org", "acme", "--project", "support",
                         "--scopes", "read", "--url", "http://127.0.0.1:5173/"]) == 0
        url = capsys.readouterr().out.strip()
        assert url.startswith("http://127.0.0.1:5173/login/link#token=")
        assert cli_main(["dashboard-link", "--org", "acme", "--project", "support",
                         "--scopes", "ingest"]) == 1
        response = sign_in_with_link(harness, url.split("#token=")[1])
        assert response.status_code == 200 and response.json()["scopes"] == ["read"]
        assert cli_main(["revoke-sessions", "--org", "acme", "--project", "support"]) == 0
    finally:
        get_settings.cache_clear()
    assert harness.client.get("/v1/traces").status_code == 401
