"""Postgres-backed job queue.

Lifecycle: ``queued`` → claimed (``running``, lock owner + time) → ``done``,
or back to ``queued`` with exponential backoff on failure, or ``dead`` after
``max_attempts``. A handler's writes and the ``done`` transition commit in the
same transaction, so a crash mid-job leaves no partial effects; the job is
re-claimed after the visibility timeout.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection, text

MAX_ERROR_CHARS = 2000


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    org_id: uuid.UUID | None
    project_id: uuid.UUID | None
    attempts: int
    max_attempts: int


def claim(conn: Connection, worker_id: str, *, limit: int = 10) -> list[ClaimedJob]:
    rows = conn.execute(text(
        "UPDATE jobs SET status = 'running', locked_at = now(), locked_by = :w, "
        "attempts = attempts + 1, updated_at = now() "
        "WHERE id IN (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now() "
        "             ORDER BY priority DESC, run_after, id LIMIT :n FOR UPDATE SKIP LOCKED) "
        "RETURNING id, kind, payload, org_id, project_id, attempts, max_attempts"),
        {"w": worker_id, "n": limit}).mappings().all()
    return [ClaimedJob(**r) for r in sorted(rows, key=lambda r: r["id"])]


def mark_done(conn: Connection, job: ClaimedJob, worker_id: str) -> bool:
    """Finish a job we still own. False if the lock was lost (job re-queued)."""
    result = conn.execute(text(
        "UPDATE jobs SET status = 'done', finished_at = now(), updated_at = now(), "
        "last_error = NULL WHERE id = :id AND status = 'running' AND locked_by = :w"),
        {"id": job.id, "w": worker_id})
    return result.rowcount == 1


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** (attempts - 1), 600))


def mark_failed(conn: Connection, job: ClaimedJob, worker_id: str, error: str) -> str:
    """Re-queue with backoff, or mark dead after the last attempt. Returns the new status."""
    status = "dead" if job.attempts >= job.max_attempts else "queued"
    conn.execute(text(
        "UPDATE jobs SET status = :s, last_error = :e, locked_at = NULL, locked_by = NULL, "
        "run_after = now() + :delay, updated_at = now(), "
        "finished_at = CASE WHEN :s = 'dead' THEN now() END "
        "WHERE id = :id AND status = 'running' AND locked_by = :w"),
        {"s": status, "e": error[:MAX_ERROR_CHARS], "delay": backoff(job.attempts),
         "id": job.id, "w": worker_id})
    return status


def requeue_stale(conn: Connection, visibility_timeout: timedelta) -> int:
    """Give back jobs whose worker stopped heartbeating (crashed or hung)."""
    result = conn.execute(text(
        "UPDATE jobs SET "
        "status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END, "
        "locked_at = NULL, locked_by = NULL, updated_at = now(), "
        "last_error = coalesce(last_error, 'lock expired') "
        "WHERE status = 'running' AND locked_at < now() - :timeout"),
        {"timeout": visibility_timeout})
    return result.rowcount


def complete_idle_traces(conn: Connection, idle_timeout: timedelta, *, limit: int = 500) -> int:
    """Complete traces whose root span never arrived, and enqueue detection for them."""
    rows = conn.execute(text(
        "UPDATE traces SET completed_at = now(), completion_reason = 'idle_timeout', "
        "updated_at = now() WHERE (project_id, trace_id) IN ("
        "  SELECT project_id, trace_id FROM traces WHERE completed_at IS NULL "
        "  AND updated_at < now() - :idle ORDER BY updated_at LIMIT :n FOR UPDATE SKIP LOCKED) "
        "RETURNING org_id, project_id, trace_id"),
        {"idle": idle_timeout, "n": limit}).all()
    for row in rows:
        conn.execute(text(
            "INSERT INTO jobs (org_id, project_id, kind, payload, dedupe_key) "
            "VALUES (:o, :p, 'trace_completed', CAST(:payload AS jsonb), :k) "
            "ON CONFLICT (dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING"),
            {"o": row.org_id, "p": row.project_id,
             "payload": json.dumps({"trace_id": row.trace_id}),
             "k": f"trace_completed:{row.project_id}:{row.trace_id}"})
    return len(rows)
