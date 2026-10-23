"""Investigation lifecycle: request → deterministic job → optional LLM job.

* ``investigate`` collects evidence, runs the rules, validates, stores the
  claims and an evidence snapshot. It completes the investigation, or hands
  over to ``investigate_llm`` when the LLM step is requested and available.
* ``investigate_llm`` reads the snapshot over a short separate connection,
  calls the model with *no transaction open*, then writes in the job's
  transaction. A slow or failing model never blocks the database or loses
  the deterministic result.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.config import Settings, get_settings
from rootsignal_server.investigation import evidence, rules
from rootsignal_server.investigation.llm import (
    LLMError,
    ModelProvider,
    build_prompt,
    estimate_tokens,
    parse_answer,
    provider_from_settings,
)
from rootsignal_server.investigation.model import Claim, EvidenceBundle
from rootsignal_server.investigation.validate import Rejected, validate
from rootsignal_server.issues.grouping import record_event
from rootsignal_server.jobs.queue import ClaimedJob

logger = logging.getLogger("rootsignal.investigation")
INVESTIGATE = "investigate"
INVESTIGATE_LLM = "investigate_llm"


def request_investigation(conn: Connection, tenant: TenantContext, issue_id: uuid.UUID, *,
                          use_llm: bool) -> uuid.UUID | None:
    """Queue an investigation of a visible issue; ``None`` if the issue isn't visible."""
    visible = conn.execute(text(
        "SELECT 1 FROM issues WHERE id = :i AND project_id = :p AND environment_id = :e"),
        {"i": issue_id, "p": tenant.project_id, "e": tenant.environment_id}).first()
    if visible is None:
        return None
    investigation_id: uuid.UUID = conn.execute(text(
        "INSERT INTO investigations (org_id, project_id, environment_id, issue_id, "
        "requested_by, use_llm) VALUES (:o, :p, :e, :i, :by, :llm) RETURNING id"),
        {"o": tenant.org_id, "p": tenant.project_id, "e": tenant.environment_id, "i": issue_id,
         "by": tenant.actor, "llm": use_llm}).scalar_one()
    _enqueue(conn, tenant.org_id, tenant.project_id, INVESTIGATE, investigation_id)
    return investigation_id


def _enqueue(conn: Connection, org_id: uuid.UUID, project_id: uuid.UUID, kind: str,
             investigation_id: uuid.UUID) -> None:
    conn.execute(text(
        "INSERT INTO jobs (org_id, project_id, kind, payload, dedupe_key, max_attempts) "
        "VALUES (:o, :p, :k, CAST(:payload AS jsonb), :d, 3)"),
        {"o": org_id, "p": project_id, "k": kind,
         "payload": json.dumps({"investigation_id": str(investigation_id)}),
         "d": f"{kind}:{investigation_id}"})


def _investigation_id(job: ClaimedJob) -> uuid.UUID:
    try:
        return uuid.UUID(str(job.payload["investigation_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{job.kind} job without a valid investigation_id") from exc


def _insert_claims(conn: Connection, investigation_id: uuid.UUID, claims: list[Claim],
                   start: int) -> None:
    for offset, claim in enumerate(claims):
        conn.execute(text(
            "INSERT INTO investigation_claims (investigation_id, position, kind, source, "
            "statement, refs) VALUES (:i, :pos, :k, :src, :st, CAST(:refs AS jsonb))"),
            {"i": investigation_id, "pos": start + offset, "k": claim.kind, "src": claim.source,
             "st": claim.statement, "refs": json.dumps(list(claim.refs))})


def _rejected_json(rejected: list[Rejected]) -> str:
    return json.dumps([{"claim": r.claim, "reason": r.reason} for r in rejected])


def _complete(conn: Connection, investigation_id: uuid.UUID) -> None:
    row = conn.execute(text(
        "UPDATE investigations SET status = 'completed', completed_at = now() WHERE id = :i "
        "RETURNING org_id, project_id, issue_id, requested_by, llm_status"),
        {"i": investigation_id}).one()
    hypotheses = conn.execute(text(
        "SELECT count(*) FROM investigation_claims WHERE investigation_id = :i "
        "AND kind = 'hypothesis'"), {"i": investigation_id}).scalar_one()
    record_event(conn, org_id=row.org_id, project_id=row.project_id, issue_id=row.issue_id,
                 kind="investigated", actor=row.requested_by,
                 details={"investigation_id": str(investigation_id), "hypotheses": hypotheses,
                          "llm_status": row.llm_status})


def _fail(conn: Connection, investigation_id: uuid.UUID, error: str) -> None:
    conn.execute(text(
        "UPDATE investigations SET status = 'failed', error = :err, completed_at = now() "
        "WHERE id = :i"), {"i": investigation_id, "err": error[:2000]})


def _llm_budget(conn: Connection, project_id: uuid.UUID) -> tuple[int, int]:
    """(monthly budget, tokens used this calendar month, UTC)."""
    row = conn.execute(text(
        "SELECT p.llm_monthly_token_budget AS budget, "
        "coalesce((SELECT sum(u.llm_input_tokens + u.llm_output_tokens) FROM usage_daily u "
        "  WHERE u.project_id = p.id "
        "  AND u.day >= date_trunc('month', now() AT TIME ZONE 'UTC')::date), 0) AS used "
        "FROM projects p WHERE p.id = :p"), {"p": project_id}).one()
    return int(row.budget), int(row.used)


def make_handlers(settings: Settings | None = None,
                  provider_factory: Callable[[], ModelProvider | None] | None = None,
                  ) -> dict[str, Callable[[Connection, ClaimedJob], None]]:
    settings = settings or get_settings()

    def factory() -> ModelProvider | None:
        return provider_factory() if provider_factory else provider_from_settings(settings)

    llm_configured = provider_factory is not None or settings.llm_provider != "none"

    def handle_investigate(conn: Connection, job: ClaimedJob) -> None:
        investigation_id = _investigation_id(job)
        inv = conn.execute(text(
            "SELECT project_id, environment_id, issue_id, use_llm, status FROM investigations "
            "WHERE id = :i FOR UPDATE"), {"i": investigation_id}).one_or_none()
        if inv is None or inv.status in ("completed", "failed"):
            return
        conn.execute(text("UPDATE investigations SET status = 'running', started_at = now() "
                          "WHERE id = :i"), {"i": investigation_id})
        bundle = evidence.collect(conn, inv.project_id, inv.environment_id, inv.issue_id)
        if bundle is None:
            _fail(conn, investigation_id, "issue no longer exists")
            return
        claims, rejected = validate(rules.investigate(bundle), bundle)
        if rejected:  # a rule citing something it didn't collect is a bug: make it visible
            logger.error("investigation %s: %d rule claim(s) rejected: %s", investigation_id,
                         len(rejected), [r.reason for r in rejected])
        _insert_claims(conn, investigation_id, claims, 0)
        llm_status, llm_error = "not_requested", None
        if inv.use_llm:
            budget, _ = _llm_budget(conn, inv.project_id)
            if not llm_configured:
                llm_status, llm_error = "disabled", "no LLM provider is configured on the server"
            elif budget <= 0:
                llm_status, llm_error = "disabled", "this project's LLM budget is 0"
            else:
                llm_status = "queued"
        conn.execute(text(
            "UPDATE investigations SET evidence = CAST(:ev AS jsonb), summary = :summary, "
            "evidence_trace_ids = :traces, rejected_claims = CAST(:rej AS jsonb), "
            "llm_status = :ls, llm_error = :le WHERE id = :i"),
            {"i": investigation_id, "ev": json.dumps(bundle.to_dict(), default=str),
             "summary": rules.summarize(claims, bundle),
             "traces": [t.trace_id for t in bundle.traces], "rej": _rejected_json(rejected),
             "ls": llm_status, "le": llm_error})
        if llm_status == "queued":
            _enqueue(conn, job.org_id or _project_org(conn, inv.project_id), inv.project_id,
                     INVESTIGATE_LLM, investigation_id)
        else:
            _complete(conn, investigation_id)

    def handle_investigate_llm(conn: Connection, job: ClaimedJob) -> None:
        investigation_id = _investigation_id(job)
        # Read everything over a separate, short-lived connection so no transaction is
        # open on the job connection while waiting for the model.
        with conn.engine.connect() as read:
            inv = read.execute(text(
                "SELECT project_id, status, evidence FROM investigations WHERE id = :i"),
                {"i": investigation_id}).one_or_none()
            if inv is None or inv.status != "running":
                return
            rows = read.execute(text(
                "SELECT kind, statement, refs, source FROM investigation_claims "
                "WHERE investigation_id = :i ORDER BY position"), {"i": investigation_id})
            rule_claims = [Claim(r.kind, r.statement, tuple(r.refs), r.source) for r in rows]
            budget, used = _llm_budget(read, inv.project_id)
        bundle = EvidenceBundle.from_dict(inv.evidence)
        system, user = build_prompt(bundle, rule_claims)
        estimate = estimate_tokens(system, user, settings.llm_max_output_tokens)
        if used + estimate > budget:
            _finish_llm(conn, investigation_id, "budget_exhausted",
                        f"needs ~{estimate} tokens; {max(budget - used, 0)} left this month")
            return
        try:
            provider = factory()
            if provider is None:
                raise LLMError("no LLM provider is configured on the server")
            completion = provider.complete(system, user, max_tokens=settings.llm_max_output_tokens)
            proposed = parse_answer(completion.text)
        except LLMError as exc:
            _finish_llm(conn, investigation_id, "failed", str(exc))
            return
        accepted, rejected = validate(proposed, bundle)
        accepted = [c for c in accepted
                    if (c.kind, c.statement.lower()) not in {(r.kind, r.statement.lower())
                                                             for r in rule_claims}]
        _insert_claims(conn, investigation_id, accepted, len(rule_claims))
        conn.execute(text(
            "INSERT INTO usage_daily "
            "(project_id, day, org_id, llm_input_tokens, llm_output_tokens) "
            "SELECT :p, (now() AT TIME ZONE 'UTC')::date, org_id, :inp, :out FROM projects "
            "WHERE id = :p ON CONFLICT (project_id, day) DO UPDATE SET "
            "llm_input_tokens = usage_daily.llm_input_tokens + EXCLUDED.llm_input_tokens, "
            "llm_output_tokens = usage_daily.llm_output_tokens + EXCLUDED.llm_output_tokens"),
            {"p": inv.project_id, "inp": completion.input_tokens,
             "out": completion.output_tokens})
        conn.execute(text(
            "UPDATE investigations SET llm_provider = :prov, llm_model = :model, "
            "llm_input_tokens = :inp, llm_output_tokens = :out, "
            "rejected_claims = rejected_claims || CAST(:rej AS jsonb) WHERE id = :i"),
            {"i": investigation_id, "prov": provider.name, "model": provider.model,
             "inp": completion.input_tokens, "out": completion.output_tokens,
             "rej": _rejected_json(rejected)})
        _finish_llm(conn, investigation_id, "completed", None)

    return {INVESTIGATE: handle_investigate, INVESTIGATE_LLM: handle_investigate_llm}


def _project_org(conn: Connection, project_id: uuid.UUID) -> uuid.UUID:
    org_id: uuid.UUID = conn.execute(text("SELECT org_id FROM projects WHERE id = :p"),
                                     {"p": project_id}).scalar_one()
    return org_id


def _finish_llm(conn: Connection, investigation_id: uuid.UUID, status: str,
                error: str | None) -> None:
    conn.execute(text("UPDATE investigations SET llm_status = :s, llm_error = :e WHERE id = :i"),
                 {"i": investigation_id, "s": status, "e": error})
    _complete(conn, investigation_id)


# --- reads -------------------------------------------------------------------------

_INVESTIGATION_COLUMNS = (
    "id, issue_id, status, requested_by, use_llm, llm_status, llm_provider, llm_model, "
    "llm_input_tokens, llm_output_tokens, llm_error, summary, evidence_trace_ids, "
    "rejected_claims, error, created_at, started_at, completed_at"
)


def list_investigations(conn: Connection, tenant: TenantContext,
                        issue_id: uuid.UUID) -> list[dict[str, Any]] | None:
    visible = conn.execute(text(
        "SELECT 1 FROM issues WHERE id = :i AND project_id = :p AND environment_id = :e"),
        {"i": issue_id, "p": tenant.project_id, "e": tenant.environment_id}).first()
    if visible is None:
        return None
    rows = conn.execute(text(
        f"SELECT {_INVESTIGATION_COLUMNS} FROM investigations WHERE issue_id = :i "
        "AND project_id = :p AND environment_id = :e ORDER BY created_at DESC LIMIT 20"),
        {"i": issue_id, "p": tenant.project_id, "e": tenant.environment_id}).mappings().all()
    return [dict(r) for r in rows]


def get_investigation(conn: Connection, tenant: TenantContext,
                      investigation_id: uuid.UUID) -> dict[str, Any] | None:
    row = conn.execute(text(
        f"SELECT {_INVESTIGATION_COLUMNS} FROM investigations WHERE id = :i "
        "AND project_id = :p AND environment_id = :e"),
        {"i": investigation_id, "p": tenant.project_id, "e": tenant.environment_id}
    ).mappings().one_or_none()
    if row is None:
        return None
    claims = conn.execute(text(
        "SELECT kind, source, statement, refs FROM investigation_claims "
        "WHERE investigation_id = :i ORDER BY "
        "array_position(ARRAY['observed','hypothesis','alternative','missing_evidence',"
        "'next_step','verified'], kind), position"), {"i": investigation_id}).mappings().all()
    return {**dict(row), "claims": [dict(c) for c in claims]}
