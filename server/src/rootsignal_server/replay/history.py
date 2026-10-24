"""What replays have shown so far: each case's latest run, and verified fixes.

A fix counts as verified when a case is ``passing``: a verify run passed after
the case had reproduced the failure (see ``verdict.next_case_status``). Only
this module turns that into ``verified`` investigation claims (source
``replay``), as the claims taxonomy requires.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.investigation.model import Claim, trace_ref

# slot → which runs it picks; the newest matching evaluated run per case wins.
_SLOTS = {
    "latest_replay": "r.verdict IS NOT NULL",
    "verified_by": "r.purpose = 'verify' AND r.verdict = 'passing'",
    "reproduced_by": "r.purpose = 'reproduce' AND r.verdict = 'failing'",
}


def case_replay_state(conn: Connection,
                      case_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """``{case_id: {latest_replay, verified_by, reproduced_by}}``, each a run brief or None."""
    state: dict[uuid.UUID, dict[str, Any]] = {
        c: dict.fromkeys(_SLOTS) for c in case_ids}
    if not case_ids:
        return state
    selects = [
        f"(SELECT DISTINCT ON (r.case_id) '{slot}' AS slot, r.case_id, r.id AS run_id, "
        "r.purpose, r.mode, r.verdict, r.trace_id, r.evaluated_at, t.agent_version "
        "FROM replay_runs r LEFT JOIN traces t "
        "ON t.project_id = r.project_id AND t.trace_id = r.trace_id "
        f"WHERE r.case_id = ANY(:ids) AND {condition} "
        "ORDER BY r.case_id, r.evaluated_at DESC NULLS LAST, r.created_at DESC)"
        for slot, condition in _SLOTS.items()]
    rows = conn.execute(text(" UNION ALL ".join(selects)), {"ids": list(case_ids)}).mappings()
    for row in rows:
        state[row["case_id"]][row["slot"]] = {
            k: row[k] for k in ("run_id", "purpose", "mode", "verdict", "trace_id",
                                "evaluated_at", "agent_version")}
    return state


def _verified_claim(name: str, case_id: uuid.UUID, state: dict[str, Any]) -> Claim | None:
    verified, reproduced = state["verified_by"], state["reproduced_by"]
    if verified is None:
        return None
    before = f" on agent {reproduced['agent_version']}" if reproduced and \
        reproduced["agent_version"] else ""
    after = f" on agent {verified['agent_version']}" if verified["agent_version"] else ""
    refs = [trace_ref(verified["trace_id"])]
    if reproduced:
        refs.append(trace_ref(reproduced["trace_id"]))
    refs.append({"type": "case", "case_id": str(case_id)})
    return Claim("verified", f"Fix verified by replay: regression case “{name}” reproduced the "
                 f"failure{before} and passes{after} ({verified['mode']} replay).",
                 tuple(refs), source="replay")


def verified_claims(conn: Connection, project_id: uuid.UUID, issue_id: uuid.UUID,
                    case_id: uuid.UUID | None = None) -> list[Claim]:
    """``verified`` claims for the issue's passing cases (or just ``case_id``)."""
    cases = conn.execute(text(
        "SELECT id, name FROM regression_cases WHERE project_id = :p AND issue_id = :i "
        "AND status = 'passing' AND (CAST(:c AS uuid) IS NULL OR id = :c) ORDER BY created_at"),
        {"p": project_id, "i": issue_id, "c": case_id}).all()
    state = case_replay_state(conn, [c.id for c in cases])
    claims = [_verified_claim(c.name, c.id, state[c.id]) for c in cases]
    return [c for c in claims if c is not None]


def add_to_latest_investigation(conn: Connection, project_id: uuid.UUID, issue_id: uuid.UUID,
                                case_id: uuid.UUID) -> bool:
    """Append the case's ``verified`` claim to the issue's latest completed investigation."""
    investigation_id = conn.execute(text(
        "SELECT id FROM investigations WHERE project_id = :p AND issue_id = :i "
        "AND status = 'completed' ORDER BY created_at DESC LIMIT 1 FOR UPDATE"),
        {"p": project_id, "i": issue_id}).scalar_one_or_none()
    claims = verified_claims(conn, project_id, issue_id, case_id)
    if investigation_id is None or not claims:
        return False
    claim = claims[0]
    exists = conn.execute(text(
        "SELECT 1 FROM investigation_claims WHERE investigation_id = :i AND kind = 'verified' "
        "AND statement = :s"), {"i": investigation_id, "s": claim.statement}).first()
    if exists:
        return False
    conn.execute(text(
        "INSERT INTO investigation_claims (investigation_id, position, kind, source, statement, "
        "refs) SELECT :i, coalesce(max(position) + 1, 0), :k, :src, :st, CAST(:refs AS jsonb) "
        "FROM investigation_claims WHERE investigation_id = :i"),
        {"i": investigation_id, "k": claim.kind, "src": claim.source, "st": claim.statement,
         "refs": json.dumps(list(claim.refs))})
    return True
