"""Integration fixtures against a real local PostgreSQL test database.

Set ``ROOTSIGNAL_TEST_DATABASE_URL`` to override. The database name must end in
``_test``: the suite drops and recreates the ``public`` schema.
"""

from __future__ import annotations

import gzip
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from rootsignal_server import admin
from rootsignal_server.api.app import create_app
from rootsignal_server.config import Settings

PROTOBUF = "application/x-protobuf"

TEST_DATABASE_URL = os.environ.get(
    "ROOTSIGNAL_TEST_DATABASE_URL", "postgresql+psycopg://localhost:5432/rootsignal_test"
)
SERVER_DIR = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def reset_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    database = make_url(TEST_DATABASE_URL).database or ""
    if not database.endswith("_test"):
        pytest.exit(f"refusing to run destructive tests against database {database!r}", 2)
    # A leaked transaction would otherwise make the next TRUNCATE wait forever.
    eng = create_engine(TEST_DATABASE_URL,
                        connect_args={"options": "-c lock_timeout=10s -c timezone=UTC"})
    reset_schema(eng)
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is rolled back after each test."""
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture
def session(conn: Connection) -> Iterator[Session]:
    with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
        yield s


@dataclass(frozen=True)
class Tenant:
    org_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID


def make_tenant(conn: Connection, slug: str) -> Tenant:
    org_id = conn.execute(
        text("INSERT INTO organizations (slug, name) VALUES (:s, :s) RETURNING id"), {"s": slug}
    ).scalar_one()
    project_id = conn.execute(
        text("INSERT INTO projects (org_id, slug, name) VALUES (:o, 'p', 'P') RETURNING id"),
        {"o": org_id},
    ).scalar_one()
    environment_id = conn.execute(
        text("INSERT INTO environments (org_id, project_id, name) "
             "VALUES (:o, :p, 'production') RETURNING id"),
        {"o": org_id, "p": project_id},
    ).scalar_one()
    return Tenant(org_id, project_id, environment_id)


# ---------------------------------------------------------------------------
# API fixtures. The API commits through its own connections, so these tests
# can't use the rollback-per-test ``conn`` fixture; they truncate instead.
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_db(engine: Engine) -> Iterator[Engine]:
    def truncate() -> None:
        with engine.begin() as c:
            c.execute(text("TRUNCATE organizations, jobs, audit_events RESTART IDENTITY CASCADE"))

    truncate()
    yield engine
    truncate()


# ---------------------------------------------------------------------------
# API harness
# ---------------------------------------------------------------------------

@dataclass
class Harness:
    engine: Engine
    client: TestClient

    def tenant(self, org: str = "acme", project: str = "support",
               environment: str = "development") -> admin.TenantIds:
        with self.engine.begin() as conn:
            return admin.ensure_tenant(conn, org_slug=org, project_slug=project,
                                       environment=environment, actor="test")

    def key(self, tenant: admin.TenantIds, *scopes: str) -> str:
        with self.engine.begin() as conn:
            return admin.create_api_key(conn, tenant, name="test", scopes=scopes or ("ingest",),
                                        actor="test").plaintext

    def post(self, request: ExportTraceServiceRequest, key: str | None, *,
             compress: bool = False, content_type: str = PROTOBUF) -> httpx.Response:
        body = request.SerializeToString()
        headers = {"content-type": content_type}
        if compress:
            body = gzip.compress(body)
            headers["content-encoding"] = "gzip"
        if key is not None:
            headers["authorization"] = f"Bearer {key}"
        return self.client.post("/v1/traces", content=body, headers=headers)

    def get(self, path: str, key: str) -> httpx.Response:
        return self.client.get(path, headers={"authorization": f"Bearer {key}"})

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()

    def set_project(self, tenant: admin.TenantIds, **columns: Any) -> None:
        assignments = ", ".join(
            f"{c} = CAST(:{c} AS jsonb)" if c in ("detector_config", "issue_grouping")
            else f"{c} = :{c}"
            for c in columns)
        with self.engine.begin() as conn:
            conn.execute(text(f"UPDATE projects SET {assignments} WHERE id = :id"),
                         {**columns, "id": tenant.project_id})


def make_harness(engine: Engine, **settings: Any) -> Harness:
    app = create_app(Settings(database_url=TEST_DATABASE_URL, **settings), engine=engine)
    return Harness(engine, TestClient(app))


@pytest.fixture
def harness(clean_db: Engine) -> Harness:
    return make_harness(clean_db)




@pytest.fixture
def live_server(clean_db: Engine) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Settings(database_url=TEST_DATABASE_URL), engine=clean_db)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


