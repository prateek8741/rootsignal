"""Replay runs: register, receive the child's report, and finalise into a verdict.

Finalising needs both the runtime report (from the runner) and the completed
replay trace (from ingest + detection), and runs from whichever arrives last.
The trace must carry this run's ID *and* belong to the run's project and
environment; a trace claiming someone else's run is ignored.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.detection.store import load_config, load_trace
from rootsignal_server.issues.grouping import record_event
from rootsignal_server.regression.spec import CaseSpec
from rootsignal_server.replay.verdict import judge, next_case_status

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


class NotReplayable(ValueError):
    pass


def register_run(conn: Connection, tenant: TenantContext, case_id: uuid.UUID, *, purpose: str,
                 mode: str, matching: str) -> dict[str, Any] | None:
    row = conn.execute(text(
        "SELECT c.current_version, v.spec FROM regression_cases c "
        "JOIN regression_case_versions v ON v.case_id = c.id AND v.version = c.current_version "
        "WHERE c.id = :c AND c.project_id = :p AND c.environment_id = :e"),
        {"c": case_id, "p": tenant.project_id, "e": tenant.environment_id}).one_or_none()
    if row is None:
        return None
    spec = row.spec
    if not spec.get("entrypoint"):
        raise NotReplayable("the case has no entrypoint; set one before replaying")
    if spec.get("input", {}).get("kind") == "missing":
        raise NotReplayable("the case has no input; supply it before replaying")
    run_id: uuid.UUID = conn.execute(text(
        "INSERT INTO replay_runs (org_id, project_id, environment_id, case_id, case_version, "
        "purpose, mode, matching, requested_by) VALUES (:o, :p, :e, :c, :v, :purpose, :mode, "
        ":match, :by) RETURNING id"),
        {"o": tenant.org_id, "p": tenant.project_id, "e": tenant.environment_id, "c": case_id,
         "v": row.current_version, "purpose": purpose, "mode": mode, "match": matching,
         "by": tenant.actor}).scalar_one()
    return {"run_id": run_id, "case_id": case_id, "case_version": row.current_version,
            "spec": spec}


def submit_report(conn: Connection, tenant: TenantContext, run_id: uuid.UUID, *,
                  trace_id: str | None, report: dict[str, Any], crashed: str | None) -> bool:
    run = conn.execute(text(
        "SELECT status FROM replay_runs WHERE id = :r AND project_id = :p "
        "AND environment_id = :e FOR UPDATE"),
        {"r": run_id, "p": tenant.project_id, "e": tenant.environment_id}).one_or_none()
    if run is None:
        return False
    if run.status != "running":
        raise NotReplayable(f"this run already has a report (status {run.status})")
    if trace_id is not None and not TRACE_ID.match(trace_id):
        raise NotReplayable("trace_id must be 32 lowercase hex characters")
    if crashed or trace_id is None:
        reason = crashed or "the replay produced no trace (is the entrypoint an @rs.agent_run?)"
        conn.execute(text(
            "UPDATE replay_runs SET status = 'error', verdict = 'crashed', summary = :s, "
            "report = CAST(:rep AS jsonb), reported_at = now(), evaluated_at = now() "
            "WHERE id = :r"), {"r": run_id, "s": reason[:1000], "rep": json.dumps(report)})
        return True
    conn.execute(text(
        "UPDATE replay_runs SET status = 'awaiting_trace', trace_id = :t, "
        "report = CAST(:rep AS jsonb), reported_at = now() WHERE id = :r"),
        {"r": run_id, "t": trace_id, "rep": json.dumps(report)})
    finalize(conn, run_id)
    return True


def finalize_for_trace(conn: Connection, project_id: uuid.UUID, trace_id: str) -> None:
    """Called after detection on a completed replay trace."""
    run_id = conn.execute(text(
        "SELECT r.id FROM traces t JOIN replay_runs r ON r.id = t.replay_run_id "
        "AND r.project_id = t.project_id AND r.environment_id = t.environment_id "
        "WHERE t.project_id = :p AND t.trace_id = :t"),
        {"p": project_id, "t": trace_id}).scalar_one_or_none()
    if run_id is not None:
        finalize(conn, run_id)


def finalize(conn: Connection, run_id: uuid.UUID) -> bool:
    run = conn.execute(text(
        "SELECT r.*, c.status AS case_status, c.issue_id FROM replay_runs r "
        "JOIN regression_cases c ON c.id = r.case_id WHERE r.id = :r FOR UPDATE OF r, c"),
        {"r": run_id}).mappings().one_or_none()
    if run is None or run["status"] != "awaiting_trace" or run["trace_id"] is None:
        return False
    ready = conn.execute(text(
        "SELECT 1 FROM traces WHERE project_id = :p AND environment_id = :e AND trace_id = :t "
        "AND replay_run_id = :r AND completed_at IS NOT NULL"),
        {"p": run["project_id"], "e": run["environment_id"], "t": run["trace_id"],
         "r": run_id}).first()
    if ready is None:
        return False  # the trace hasn't arrived/completed yet: detection will call back
    loaded = load_trace(conn, run["project_id"], run["trace_id"])
    if loaded is None:
        return False
    _, trace = loaded
    spec = CaseSpec.model_validate(conn.execute(text(
        "SELECT spec FROM regression_case_versions WHERE case_id = :c AND version = :v"),
        {"c": run["case_id"], "v": run["case_version"]}).scalar_one())
    outcome = judge(spec.assertions, trace, mode=run["mode"], report=run["report"],
                    config=load_config(conn, run["project_id"]))
    new_status, note = next_case_status(run["case_status"], run["purpose"], outcome.verdict)
    summary = outcome.summary + (f" {note}" if note else "")
    conn.execute(text(
        "UPDATE replay_runs SET status = 'evaluated', verdict = :v, summary = :s, "
        "results = CAST(:res AS jsonb), evaluated_at = now() WHERE id = :r"),
        {"r": run_id, "v": outcome.verdict, "s": summary, "res": json.dumps(outcome.results)})
    conn.execute(text("UPDATE regression_cases SET status = :s, updated_at = now() WHERE id = :c"),
                 {"s": new_status, "c": run["case_id"]})
    record_event(conn, org_id=run["org_id"], project_id=run["project_id"],
                 issue_id=run["issue_id"], kind="replayed", actor=run["requested_by"],
                 details={"case_id": str(run["case_id"]), "run_id": str(run_id),
                          "purpose": run["purpose"], "mode": run["mode"],
                          "verdict": outcome.verdict, "case_status": new_status,
                          "trace_id": run["trace_id"]})
    return True


_RUN_COLUMNS = ("id, case_id, case_version, purpose, mode, matching, status, verdict, summary, "
                "requested_by, trace_id, report, results, created_at, reported_at, evaluated_at")


def get_run(conn: Connection, tenant: TenantContext, run_id: uuid.UUID) -> dict[str, Any] | None:
    row = conn.execute(text(
        f"SELECT {_RUN_COLUMNS} FROM replay_runs WHERE id = :r AND project_id = :p "
        "AND environment_id = :e"),
        {"r": run_id, "p": tenant.project_id, "e": tenant.environment_id}).mappings().one_or_none()
    return dict(row) if row else None


def list_runs(conn: Connection, tenant: TenantContext,
              case_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        f"SELECT {_RUN_COLUMNS} FROM replay_runs WHERE case_id = :c AND project_id = :p "
        "AND environment_id = :e ORDER BY created_at DESC LIMIT 50"),
        {"c": case_id, "p": tenant.project_id, "e": tenant.environment_id}).mappings().all()
    return [dict(r) for r in rows]
