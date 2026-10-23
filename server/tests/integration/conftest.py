"""Integration fixtures against a real local PostgreSQL test database.

Set ``ROOTSIGNAL_TEST_DATABASE_URL`` to override. The database name must end in
``_test``: the suite drops and recreates the ``public`` schema.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

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
