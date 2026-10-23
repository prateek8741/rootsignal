from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import text

import rootsignal as rs
from conftest import Harness
from otlp_factory import record_sdk_spans, request_from
from rootsignal_server.admin import TenantIds
from rootsignal_server.jobs.worker import Worker, default_handlers
from rootsignal_server.regression.spec import CaseSpec


class UpstreamTimeout(Exception):
    pass


@rs.tool(side_effect="read")
def get_account(account_id: str) -> dict[str, str]:
    raise UpstreamTimeout(f"account service timed out for {account_id}")


@rs.tool(side_effect="write")
def create_ticket(summary: str) -> str:
    return "T-1"


@rs.agent_run("support-agent", agent_version="v1", max_steps=4)
def support_agent(request: str, account_id: str) -> str:
    """Module level, so the SDK records an importable entrypoint for replay."""
    for _ in range(4):
        with rs.llm_call("m", provider="ollama",
                         input_messages=[{"role": "user", "content": request}]) as c:
            c.set_response([{"role": "assistant", "content": "checking"}], input_tokens=10,
                           output_tokens=2)
        with contextlib.suppress(UpstreamTimeout):
            get_account(account_id)
    create_ticket("escalate")
    return "I've updated your account."


def ingest(h: Harness, key: str, build: Callable[[], Any], **config: Any) -> str:
    spans = record_sdk_spans(build, **config)
    assert h.post(request_from(spans), key).status_code == 200
    worker = Worker(h.engine, default_handlers(), maintenance_interval_s=3600)
    while worker.run_once():
        pass
    return format(spans[0].context.trace_id, "032x")


def issue_id(h: Harness, tenant: TenantIds, detector: str) -> uuid.UUID:
    with h.engine.connect() as conn:
        found: uuid.UUID = conn.execute(text(
            "SELECT id FROM issues WHERE detector = :d AND environment_id = :e"),
            {"d": detector, "e": tenant.environment_id}).scalar_one()
    return found


def create(h: Harness, key: str, issue: uuid.UUID, **body: Any) -> Any:
    return h.client.post(f"/v1/issues/{issue}/regression-cases", json=body,
                         headers={"authorization": f"Bearer {key}"})


def patch(h: Harness, key: str, case_id: str, **body: Any) -> Any:
    return h.client.patch(f"/v1/regression-cases/{case_id}", json=body,
                          headers={"authorization": f"Bearer {key}"})


def test_case_from_a_fully_captured_run(harness: Harness) -> None:
    tenant = harness.tenant()
    triage = harness.key(tenant, "read", "triage")
    trace_id = ingest(harness, harness.key(tenant),
                      lambda: support_agent("upgrade my plan", "ent-42"), capture_user_input=True)

    response = create(harness, triage, issue_id(harness, tenant, "repeated_tool_call"))
    assert response.status_code == 201, response.text
    case = response.json()
    assert (case["status"], case["current_version"], case["source_trace_id"]) == \
        ("draft", 1, trace_id)
    assert case["entrypoint"].endswith(":support_agent")
    spec = CaseSpec.model_validate(case["spec"])
    assert spec.input.kind == "kwargs"
    assert spec.input.value == {"request": "upgrade my plan", "account_id": "ent-42"}

    lookups = [t for t in spec.fixtures.tools if t.tool == "get_account"]
    assert len(lookups) == 4 and {t.outcome for t in lookups} == {"error"}
    assert lookups[0].arguments.value == {"account_id": "ent-42"}
    assert lookups[0].error_type == "UpstreamTimeout"
    ticket = next(t for t in spec.fixtures.tools if t.tool == "create_ticket")
    assert ticket.side_effect == "write" and ticket.result is not None
    assert len(spec.fixtures.llm) == 4 and spec.fixtures.llm[0].output.captured

    reproduces = [a for a in spec.assertions if a.role == "reproduces"]
    assert [(a.kind, a.params, a.requires) for a in reproduces] == [
        ("max_identical_tool_calls", {"tool": "get_account", "max": 2}, "recorded")]
    assert {a.kind for a in spec.assertions if a.role == "guard"} == \
        {"run_completed", "no_unhandled_exception"}
    assert spec.quality.replayable_recorded
    assert any("stub" in w for w in spec.quality.warnings)  # create_ticket is a write
    assert not any("input" in w for w in spec.quality.warnings)


def test_missing_input_is_flagged_and_supplied_in_a_new_version(harness: Harness) -> None:
    tenant = harness.tenant()
    triage = harness.key(tenant, "read", "triage")
    ingest(harness, harness.key(tenant), lambda: support_agent("hi", "ent-1"))  # default capture
    case = create(harness, triage, issue_id(harness, tenant, "repeated_tool_call")).json()
    assert case["spec"]["input"]["kind"] == "missing" and not case["replayable_recorded"]
    assert any("input wasn't captured" in w for w in case["spec"]["quality"]["warnings"])

    edited = patch(harness, triage, case["id"], input={"request": "hi", "account_id": "ent-1"},
                   note="input from the support ticket")
    assert edited.status_code == 200, edited.text
    v2 = edited.json()
    assert v2["current_version"] == 2 and v2["replayable_recorded"]
    assert v2["spec"]["input"] == {"kind": "kwargs", "value": {"request": "hi",
                                   "account_id": "ent-1"}, "supplied_by_user": True,
                                   "redacted": False, "truncated": False}
    assert [v["version"] for v in v2["versions"]] == [2, 1]
    assert v2["versions"][0]["change_note"] == "input from the support ticket"

    v1 = harness.get(f"/v1/regression-cases/{case['id']}/spec?version=1", triage).json()
    assert v1["version"] == 1 and v1["input"]["kind"] == "missing"  # history is immutable
    current = harness.get(f"/v1/regression-cases/{case['id']}/spec", triage).json()
    assert current["case_id"] == case["id"] and current["version"] == 2
    CaseSpec.model_validate({k: v for k, v in current.items() if k not in ("case_id", "version")})


def test_disabling_every_reproducing_assertion_is_flagged(harness: Harness) -> None:
    tenant = harness.tenant()
    triage = harness.key(tenant, "read", "triage")
    ingest(harness, harness.key(tenant), lambda: support_agent("hi", "ent-1"))
    case = create(harness, triage, issue_id(harness, tenant, "repeated_tool_call")).json()
    reproducing = [a["id"] for a in case["spec"]["assertions"] if a["role"] == "reproduces"]
    edited = patch(harness, triage, case["id"], disabled_assertions=reproducing).json()
    warnings = edited["spec"]["quality"]["warnings"]
    assert any("No enabled assertion reproduces" in w for w in warnings)
    assert patch(harness, triage, case["id"], disabled_assertions=["a99"]).status_code == 422
    assert patch(harness, triage, case["id"]).status_code == 422  # nothing to change
    assert patch(harness, triage, case["id"], entrypoint="not an entrypoint").status_code == 422
    assert patch(harness, triage, case["id"], entrypoint="pkg.agents:run").status_code == 200


def test_tool_failures_need_live_tools_and_truncation_is_reported(harness: Harness) -> None:
    tenant = harness.tenant()
    harness.set_project(tenant, max_payload_bytes=1024)
    triage = harness.key(tenant, "read", "triage")

    @rs.agent_run("export-agent")
    def export() -> None:
        with rs.tool_call("read_report", arguments={"id": 1}, side_effect="read") as t:
            t.set_result("x" * 5000)
        with contextlib.suppress(PermissionError), rs.tool_call(
                "upload", arguments={"id": 1}, side_effect="write"):
            raise PermissionError("bucket is read-only")

    ingest(harness, harness.key(tenant), export)
    spec = create(harness, triage, issue_id(harness, tenant, "tool_error")).json()["spec"]
    reproduces = [a for a in spec["assertions"] if a["role"] == "reproduces"]
    assert [(a["kind"], a["params"], a["requires"]) for a in reproduces] == [
        ("detector_absent", {"detector": "tool_error", "tool": "upload"}, "live_tools")]
    assert spec["entrypoint"] is None  # nested function: not importable
    warnings = " | ".join(spec["quality"]["warnings"])
    assert "truncated payloads" in warnings and "No entrypoint" in warnings
    assert not spec["quality"]["replayable_recorded"]


def test_sources_scopes_and_tenants(harness: Harness) -> None:
    a, b = harness.tenant("acme"), harness.tenant("globex")
    triage = harness.key(a, "read", "triage")
    first = ingest(harness, harness.key(a), lambda: support_agent("hi", "ent-1"))
    issue = issue_id(harness, a, "repeated_tool_call")

    @rs.agent_run("other")
    def healthy() -> None:
        with rs.tool_call("ping", arguments={}, side_effect="none"):
            pass

    unrelated = ingest(harness, harness.key(a), healthy)
    assert create(harness, triage, issue, trace_id=unrelated).status_code == 422
    assert create(harness, triage, issue, trace_id=first).status_code == 201
    assert create(harness, harness.key(a, "read"), issue).status_code == 403
    assert create(harness, harness.key(b, "read", "triage"), issue).status_code == 404

    case_id = harness.get(f"/v1/issues/{issue}/regression-cases", triage).json()["items"][0]["id"]
    other = harness.key(b, "read", "triage")
    assert harness.get(f"/v1/regression-cases/{case_id}", other).status_code == 404
    assert harness.get(f"/v1/regression-cases/{case_id}/spec", other).status_code == 404
    assert patch(harness, other, case_id, name="x").status_code == 404
    assert harness.get("/v1/regression-cases", other).json()["items"] == []
    assert len(harness.get("/v1/regression-cases?status=draft", triage).json()["items"]) == 1
