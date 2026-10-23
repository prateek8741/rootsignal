from __future__ import annotations

import contextlib

import rootsignal as rs
from conftest import Harness
from otlp_factory import sdk_request
from rootsignal_server.jobs.worker import Worker, default_handlers


def agent(version: str, fail: bool) -> None:
    with (rs.agent_run("support-agent", agent_version=version),
          contextlib.suppress(RuntimeError),
          rs.tool_call("lookup", arguments={"v": version}, side_effect="read")):
        if fail:
            raise RuntimeError("boom")


def test_context_describes_the_key(harness: Harness) -> None:
    tenant = harness.tenant("acme", "support", "staging")
    key = harness.key(tenant, "read", "triage")
    context = harness.get("/v1/context", key).json()
    assert context == {"org_slug": "acme", "org_name": "acme", "project_slug": "support",
                       "project_name": "support", "environment": "staging",
                       "scopes": ["read", "triage"], "key_prefix": key[:11],
                       "principal": f"api_key:{key[:11]}", "llm_available": False}
    ingest_only = harness.key(tenant, "ingest")
    assert harness.get("/v1/context", ingest_only).json()["scopes"] == ["ingest"]
    assert harness.get("/v1/context", "rs_" + "x" * 43).status_code == 401


def test_trace_version_filter_and_finding_count(harness: Harness) -> None:
    tenant = harness.tenant()
    key, read = harness.key(tenant), harness.key(tenant, "read")
    harness.post(sdk_request(lambda: agent("v1", fail=True)), key)
    harness.post(sdk_request(lambda: agent("v2", fail=False)), key)
    worker = Worker(harness.engine, default_handlers())
    while worker.run_once():
        pass
    v1 = harness.get("/v1/traces?agent_version=v1", read).json()["items"]
    assert len(v1) == 1 and v1[0]["agent_version"] == "v1" and v1[0]["finding_count"] == 1
    v2 = harness.get("/v1/traces?agent_version=v2", read).json()["items"]
    assert v2[0]["finding_count"] == 0
    detail = harness.get(f"/v1/traces/{v1[0]['trace_id']}", read).json()
    assert detail["trace"]["finding_count"] == len(detail["findings"]) == 1
