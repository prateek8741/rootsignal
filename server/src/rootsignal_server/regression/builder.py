"""Build a regression case spec from a stored trace. Tenant-scoped reads only."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import Connection, text

from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.model import SpanView, TraceView
from rootsignal_server.detection.store import load_config
from rootsignal_server.regression import assertions as A
from rootsignal_server.regression.spec import (
    CaseInput,
    CaseSpec,
    Fixtures,
    LLMFixture,
    Payload,
    Quality,
    RetrievalFixture,
    Source,
    ToolFixture,
)


class CaseSourceError(ValueError):
    """The requested trace can't back a case for this issue."""


_SPANS = text(
    "SELECT s.span_id, s.parent_span_id, s.kind, s.name, s.status, s.status_message, "
    "s.error_type, s.started_at, s.ended_at, s.duration_ms, s.model, s.provider, "
    "s.input_tokens, s.output_tokens, s.tool_name, s.side_effect, s.args_hash, s.call_index, "
    "s.repeat_index, s.attributes, s.events, "
    "pi.body AS in_body, pi.content_type AS in_type, pi.truncated AS in_trunc, "
    "pi.redaction_state AS in_red, "
    "po.body AS out_body, po.content_type AS out_type, po.truncated AS out_trunc, "
    "po.redaction_state AS out_red "
    "FROM spans s "
    "LEFT JOIN payloads pi ON pi.project_id = s.project_id AND pi.sha256 = s.input_payload_hash "
    "LEFT JOIN payloads po ON po.project_id = s.project_id AND po.sha256 = s.output_payload_hash "
    "WHERE s.project_id = :p AND s.environment_id = :e AND s.trace_id = :t "
    "ORDER BY s.started_at, s.call_index NULLS FIRST, s.span_id")


def _payload(body: str | None, content_type: str | None, truncated: bool | None,
             redaction: str | None) -> Payload:
    if body is None:
        return Payload(captured=False)
    value: Any = body
    if content_type == "json" and not truncated:
        value = json.loads(body)
    # "sdk"/"server" means redaction *ran*; we can't tell whether it changed this value,
    # except for the explicit markers it leaves behind.
    return Payload(captured=True, value=value, truncated=bool(truncated),
                   redacted="[REDACTED:" in body)


def build_spec(conn: Connection, *, project_id: uuid.UUID, environment_id: uuid.UUID,
               issue_id: uuid.UUID, trace_id: str, name: str,
               input_override: Any = None, has_override: bool = False) -> CaseSpec:
    scope = {"p": project_id, "e": environment_id}
    head = conn.execute(text(
        "SELECT t.trace_id, t.agent_name, t.agent_version, t.started_at, t.completion_reason "
        "FROM traces t WHERE t.project_id = :p AND t.environment_id = :e AND t.trace_id = :t "
        "AND EXISTS (SELECT 1 FROM issue_occurrences o WHERE o.issue_id = :i "
        "            AND o.trace_id = t.trace_id)"),
        {**scope, "t": trace_id, "i": issue_id}).mappings().one_or_none()
    if head is None:
        raise CaseSourceError("trace not found, or it is not an occurrence of this issue")
    rows = list(conn.execute(_SPANS, {**scope, "t": trace_id}).mappings().all())
    view = TraceView.build(trace_id, (SpanView.from_mapping(dict(r)) for r in rows))
    config: DetectorConfig = load_config(conn, project_id)
    findings = conn.execute(text(
        "SELECT f.id, f.detector, f.finding_key, f.tool_name, f.facts FROM findings f "
        "JOIN issue_occurrences o ON o.finding_id = f.id "
        "WHERE o.issue_id = :i AND f.project_id = :p AND f.trace_id = :t"),
        {**scope, "i": issue_id, "t": trace_id}).mappings().all()

    agent = next((r for r in rows if r["kind"] == "agent"), None)
    entrypoint = (agent["attributes"] or {}).get("rootsignal.run.entrypoint") if agent else None
    side_effects = {r["tool_name"]: r["side_effect"] for r in rows if r["tool_name"]}
    candidates = [assertion for f in findings for assertion in A.from_finding(
        f["detector"], str(f["id"]), f["finding_key"], f["tool_name"], f["facts"] or {},
        side_effects.get(f["tool_name"]), config)]
    candidates.extend(A.GUARDS)
    chosen, dropped = A.select(candidates, view, config)
    spec = CaseSpec(
        name=name,
        source=Source(trace_id=trace_id, issue_id=str(issue_id), agent_name=head["agent_name"],
                      agent_version=head["agent_version"],
                      started_at=head["started_at"].isoformat(),
                      completion_reason=head["completion_reason"]),
        entrypoint=entrypoint, input=_input(agent, input_override, has_override),
        fixtures=_fixtures(rows), assertions=chosen,
        quality=Quality(replayable_recorded=False, dropped_assertions=dropped))
    return spec.model_copy(update={"quality": assess(spec)})


def assess(spec: CaseSpec) -> Quality:
    """Derive replayability and warnings from the spec itself, so they stay correct
    when a new version changes the input, entrypoint or enabled assertions."""
    warnings: list[str] = []
    tools = spec.fixtures.tools
    if spec.entrypoint is None:
        warnings.append("No entrypoint recorded (the agent wasn't run through a decorated "
                        "@rs.agent_run function); set one before replaying.")
    if spec.input.kind == "missing":
        warnings.append("The run's input wasn't captured (capture_user_input is off by "
                        "default); supply it before replaying.")
    if spec.input.truncated:
        warnings.append("The run's input was truncated at capture; replay may differ.")
    if spec.input.redacted:
        warnings.append("The run's input contains redacted values; replay sees the "
                        "placeholders, not the originals.")
    missing_args = sum(1 for t in tools if not t.arguments.captured)
    missing_results = sum(1 for t in tools
                          if t.outcome == "result" and not (t.result and t.result.captured))
    if missing_args or missing_results:
        warnings.append(f"{missing_args} tool argument(s) and {missing_results} tool result(s) "
                        "weren't captured; recorded replay can't serve them.")
    truncated = sum(1 for t in tools
                    if t.arguments.truncated or (t.result is not None and t.result.truncated))
    if truncated:
        warnings.append(f"{truncated} tool call(s) have truncated payloads.")
    redacted = sum(1 for t in tools
                   if t.arguments.redacted or (t.result is not None and t.result.redacted))
    if redacted:
        warnings.append(f"{redacted} tool call(s) contain redacted values; replay sees "
                        "placeholders.")
    if any(t.side_effect in ("write", "unknown") for t in tools):
        warnings.append("The run calls write/unknown side-effect tools; replay will stub "
                        "them and never execute them.")
    uncaptured_outputs = sum(1 for f in spec.fixtures.llm if not f.output.captured)
    if uncaptured_outputs:
        warnings.append(f"{uncaptured_outputs} model output(s) weren't captured; recorded "
                        "replay of model calls won't be possible for them.")
    if spec.source.completion_reason == "idle_timeout":
        warnings.append("The source trace is partial (its root span never arrived).")
    if not any(a.role == "reproduces" and a.enabled for a in spec.assertions):
        warnings.append("No enabled assertion reproduces this failure, so a replay couldn't "
                        "show whether it is fixed.")
    replayable = (spec.entrypoint is not None and spec.input.kind != "missing"
                  and missing_args == 0 and missing_results == 0 and truncated == 0)
    return Quality(replayable_recorded=replayable, warnings=warnings,
                   dropped_assertions=spec.quality.dropped_assertions)


def _input(agent: Any, override: Any, has_override: bool) -> CaseInput:
    if has_override:
        return CaseInput(kind="kwargs" if isinstance(override, dict) else "value",
                         value=override, supplied_by_user=True)
    if agent is None or agent["in_body"] is None:
        return CaseInput(kind="missing")
    payload = _payload(agent["in_body"], agent["in_type"], agent["in_trunc"], agent["in_red"])
    return CaseInput(kind="kwargs" if isinstance(payload.value, dict) else "value",
                     value=payload.value, redacted=payload.redacted,
                     truncated=payload.truncated)


def _fixtures(rows: list[Any]) -> Fixtures:
    tools: list[ToolFixture] = []
    llm: list[LLMFixture] = []
    retrievals: list[RetrievalFixture] = []
    for row in rows:
        args = _payload(row["in_body"], row["in_type"], row["in_trunc"], row["in_red"])
        out = _payload(row["out_body"], row["out_type"], row["out_trunc"], row["out_red"])
        if row["kind"] in ("tool", "mcp"):
            failed = row["status"] == "error"
            tools.append(ToolFixture(
                call_index=row["call_index"] if row["call_index"] is not None else len(tools),
                tool=row["tool_name"] or row["name"], side_effect=row["side_effect"] or "unknown",
                args_hash=row["args_hash"], repeat_index=row["repeat_index"], arguments=args,
                outcome="error" if failed else "result", result=None if failed else out,
                error_type=row["error_type"] if failed else None,
                error_message=row["status_message"] if failed else None))
        elif row["kind"] == "llm":
            llm.append(LLMFixture(
                call_index=len(llm), model=row["model"], provider=row["provider"], output=out,
                input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
                failed=row["status"] == "error", error_type=row["error_type"]))
        elif row["kind"] == "retrieval":
            retrievals.append(RetrievalFixture(
                position=len(retrievals),
                data_source=(row["attributes"] or {}).get("gen_ai.data_source.id"),
                query=args, documents=out))
    return Fixtures(tools=tools, llm=llm, retrievals=retrievals)
