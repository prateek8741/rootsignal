from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from conftest import TEST_DATABASE_URL, Tenant, alembic_config, make_tenant
from rootsignal_server.storage.models import Base

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"


def insert_trace(conn: Connection, t: Tenant, trace_id: str = TRACE_ID, **overrides: Any) -> None:
    values = {"p": t.project_id, "o": t.org_id, "e": t.environment_id, "t": trace_id,
              **overrides}
    conn.execute(text(
        "INSERT INTO traces (project_id, trace_id, org_id, environment_id, started_at) "
        "VALUES (:p, :t, :o, :e, now())"), values)


def insert_span(conn: Connection, t: Tenant, **overrides: Any) -> None:
    values = {"p": t.project_id, "o": t.org_id, "e": t.environment_id, "t": TRACE_ID,
              "s": SPAN_ID, "kind": "tool", "side_effect": "read", **overrides}
    conn.execute(text(
        "INSERT INTO spans (project_id, trace_id, span_id, org_id, environment_id, kind, name, "
        "side_effect, started_at) VALUES (:p, :t, :s, :o, :e, :kind, 'x', :side_effect, now())"),
        values)


def expect_integrity_error(conn: Connection, fn: Any) -> None:
    savepoint = conn.begin_nested()
    with pytest.raises(IntegrityError):
        fn()
    savepoint.rollback()


def test_models_and_migrations_do_not_drift(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migrations_round_trip() -> None:
    # Separate engine so the session-scoped schema is rebuilt afterwards.
    eng = create_engine(TEST_DATABASE_URL)
    cfg = alembic_config(TEST_DATABASE_URL)
    try:
        command.downgrade(cfg, "base")
        assert set(inspect(eng).get_table_names()) == {"alembic_version"}
        command.upgrade(cfg, "head")
        assert {"organizations", "projects", "environments", "api_keys", "traces", "spans",
                "payloads", "jobs"} <= set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_same_client_ids_in_two_tenants_do_not_collide(conn: Connection) -> None:
    a, b = make_tenant(conn, "a"), make_tenant(conn, "b")
    insert_trace(conn, a)
    insert_trace(conn, b)  # identical trace_id, other tenant: allowed, separate row
    insert_span(conn, a)
    insert_span(conn, b)
    count = conn.execute(text("SELECT count(*) FROM spans WHERE trace_id = :t"),
                         {"t": TRACE_ID}).scalar_one()
    assert count == 2


def test_rows_cannot_mix_tenants(conn: Connection) -> None:
    a, b = make_tenant(conn, "a"), make_tenant(conn, "b")
    # project A with environment from project B
    expect_integrity_error(conn, lambda: insert_trace(conn, a, e=b.environment_id))
    # org B claiming project A
    expect_integrity_error(conn, lambda: insert_trace(conn, a, o=b.org_id))
    insert_trace(conn, a)
    # span pointing at tenant A's trace with tenant B's environment
    expect_integrity_error(conn, lambda: insert_span(conn, a, e=b.environment_id))
    # API key bound to project A but environment B
    expect_integrity_error(conn, lambda: conn.execute(text(
        "INSERT INTO api_keys (org_id, project_id, environment_id, name, prefix, key_hash) "
        "VALUES (:o, :p, :e, 'k', 'rs_x', 'h')"),
        {"o": a.org_id, "p": a.project_id, "e": b.environment_id}))


def test_span_requires_existing_trace(conn: Connection) -> None:
    a = make_tenant(conn, "a")
    expect_integrity_error(conn, lambda: insert_span(conn, a))


@pytest.mark.parametrize(("field", "value"), [
    ("kind", "banana"),
    ("side_effect", "sometimes"),
    ("s", "short"),
])
def test_span_check_constraints(conn: Connection, field: str, value: str) -> None:
    a = make_tenant(conn, "a")
    insert_trace(conn, a)
    expect_integrity_error(conn, lambda: insert_span(conn, a, **{field: value}))


def test_trace_id_length_is_enforced(conn: Connection) -> None:
    a = make_tenant(conn, "a")
    expect_integrity_error(conn, lambda: insert_trace(conn, a, trace_id="abc"))


def test_api_key_scopes_are_constrained(conn: Connection) -> None:
    a = make_tenant(conn, "a")

    def insert(scopes: str, prefix: str) -> None:
        conn.execute(text(
            "INSERT INTO api_keys (org_id, project_id, environment_id, name, prefix, key_hash, "
            f"scopes) VALUES (:o, :p, :e, 'k', :prefix, :prefix, {scopes})"),
            {"o": a.org_id, "p": a.project_id, "e": a.environment_id, "prefix": prefix})

    insert("ARRAY['ingest', 'read']", "rs_ok")
    expect_integrity_error(conn, lambda: insert("ARRAY['root']", "rs_bad"))
    expect_integrity_error(conn, lambda: insert("ARRAY[]::text[]", "rs_empty"))


def test_deleting_a_project_cascades_to_its_data(conn: Connection) -> None:
    a, b = make_tenant(conn, "a"), make_tenant(conn, "b")
    for t in (a, b):
        insert_trace(conn, t)
        insert_span(conn, t)
        conn.execute(text(
            "INSERT INTO payloads (project_id, sha256, org_id, byte_size, body) "
            "VALUES (:p, repeat('a', 64), :o, 1, 'x')"), {"p": t.project_id, "o": t.org_id})
    conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": a.project_id})
    for table in ("traces", "spans", "payloads", "environments"):
        remaining = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert remaining == 1, table  # only tenant B's row is left


def test_job_dedupe_applies_only_to_active_jobs(conn: Connection) -> None:
    a = make_tenant(conn, "a")

    def enqueue(status: str) -> None:
        conn.execute(text(
            "INSERT INTO jobs (org_id, project_id, kind, status, dedupe_key) "
            "VALUES (:o, :p, 'trace_completed', :s, 'trace:1')"),
            {"o": a.org_id, "p": a.project_id, "s": status})

    enqueue("done")
    enqueue("queued")  # a finished job with the same key doesn't block a new one
    expect_integrity_error(conn, lambda: enqueue("running"))


def test_job_tenant_columns_are_both_set_or_both_null(conn: Connection) -> None:
    a = make_tenant(conn, "a")
    conn.execute(text("INSERT INTO jobs (kind) VALUES ('system_retention')"))
    expect_integrity_error(conn, lambda: conn.execute(
        text("INSERT INTO jobs (org_id, kind) VALUES (:o, 'x')"), {"o": a.org_id}))



@pytest.mark.parametrize(("column", "value"), [
    ("redact_on_ingest", "'sometimes'"),
    ("max_spans_per_day", "0"),
    ("max_payload_bytes", "100"),
])
def test_project_policy_constraints(conn: Connection, column: str, value: str) -> None:
    # Added by hand in migration 0002 (autogenerate misses CHECKs on existing tables).
    a = make_tenant(conn, "a")
    expect_integrity_error(conn, lambda: conn.execute(
        text(f"UPDATE projects SET {column} = {value} WHERE id = :p"), {"p": a.project_id}))


def test_engine_factory_pins_utc_sessions() -> None:
    from rootsignal_server.config import Settings
    from rootsignal_server.storage.db import make_engine

    eng = make_engine(Settings(database_url=TEST_DATABASE_URL))
    try:
        with eng.connect() as c:
            assert c.execute(text("SHOW timezone")).scalar_one() == "UTC"
    finally:
        eng.dispose()
