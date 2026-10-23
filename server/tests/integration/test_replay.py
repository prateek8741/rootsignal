"""End-to-end replay: real API server, worker thread, and the `rootsignal replay` CLI
running the example agent in a child process."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

import rootsignal as rs
from conftest import Harness, make_harness
from rootsignal_server.jobs.worker import Worker, default_handlers

STUBS = "rootsignal_examples.support_agent.replay_stubs"


@dataclass
class Stack:
    url: str
    harness: Harness
    key: str  # read, ingest, triage, replay
    tickets: Path

    def api(self, method: str, path: str, **kwargs: Any) -> Any:
        response = httpx.request(method, self.url + path, timeout=30,
                                 headers={"authorization": f"Bearer {self.key}"}, **kwargs)
        return response

    def replay(self, case_id: str, *args: str, fixed: bool = False,
               key: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "ROOTSIGNAL_API_KEY": key or self.key,
               "ROOTSIGNAL_ENDPOINT": self.url, "SUPPORT_AGENT_TICKETS": str(self.tickets)}
        env.pop("SUPPORT_AGENT_FIX", None)
        if fixed:
            env["SUPPORT_AGENT_FIX"] = "1"
        return subprocess.run([sys.executable, "-m", "rootsignal.cli", "replay", case_id,
                               "--stubs", STUBS, "--wait", "30", *args],
                              env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def stack(clean_db: Engine, live_server: str, tmp_path: Path) -> Iterator[Stack]:
    harness = make_harness(clean_db)
    tenant = harness.tenant()
    key = harness.key(tenant, "read", "ingest", "triage", "replay")
    stop = threading.Event()
    worker = Worker(clean_db, default_handlers(), poll_interval_s=0.1,
                    maintenance_interval_s=3600)
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    yield Stack(live_server, harness, key, tmp_path / "tickets.jsonl")
    stop.set()
    thread.join(10)


def record_production_run(stack: Stack, account_id: str = "ent-42") -> None:
    """Run the buggy v1 agent in-process, exporting to the live server."""
    os.environ["SUPPORT_AGENT_TICKETS"] = str(stack.tickets)
    from rootsignal_examples.support_agent import agent

    assert not agent.FIXED
    rs.init(rs.Config(api_key=stack.key, endpoint=stack.url, capture_user_input=True))
    try:
        assert agent.handle("Please upgrade me to 20 seats", account_id) == \
            "Done! I've upgraded your plan to 20 seats."
        assert rs.flush(10)
    finally:
        rs.shutdown()


def wait_for(stack: Stack, sql: str, **params: Any) -> Any:
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with stack.harness.engine.connect() as conn:
            value = conn.execute(text(sql), params).scalar()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {sql}")


def make_case(stack: Stack, detector: str) -> str:
    issue = wait_for(stack, "SELECT id FROM issues WHERE detector = :d", d=detector)
    response = stack.api("POST", f"/v1/issues/{issue}/regression-cases", json={})
    assert response.status_code == 201, response.text
    case_id: str = response.json()["id"]
    return case_id


def latest_run(stack: Stack, case_id: str) -> dict[str, Any]:
    runs = stack.api("GET", f"/v1/regression-cases/{case_id}/replays").json()["items"]
    run: dict[str, Any] = runs[0]
    return run


def test_reproduce_then_verify_the_fix(stack: Stack) -> None:
    record_production_run(stack)
    case_id = make_case(stack, "repeated_tool_call")
    case = stack.api("GET", f"/v1/regression-cases/{case_id}").json()
    assert case["entrypoint"] == "rootsignal_examples.support_agent.agent:handle"
    assert case["replayable_recorded"]

    # 1. Before the fix: replay must fail the same way production did.
    before = stack.replay(case_id, "--purpose", "reproduce")
    assert before.returncode == 0, before.stdout + before.stderr
    assert "verdict: failing" in before.stdout and "[FAIL] a1" in before.stdout
    run = latest_run(stack, case_id)
    assert run["verdict"] == "failing" and run["status"] == "evaluated"
    assert run["report"]["served_tools"] == 4 and run["report"]["served_model_calls"] == 4
    assert stack.api("GET", f"/v1/regression-cases/{case_id}").json()["status"] == "reproduced"

    # 2. After the fix (v2: escalate instead of retrying): replay must pass.
    after = stack.replay(case_id, "--purpose", "verify", fixed=True)
    assert after.returncode == 0, after.stdout + after.stderr
    assert "verdict: passing" in after.stdout
    run = latest_run(stack, case_id)
    assert run["report"]["stubs"] == [{"tool": "create_ticket", "arguments": {
        "summary": "Account lookup failing for ent-42", "priority": "high"}}]
    assert stack.api("GET", f"/v1/regression-cases/{case_id}").json()["status"] == "passing"

    # The write tool was never executed by either replay.
    assert not stack.tickets.exists()
    # Replay traces never become production issue occurrences.
    with stack.harness.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM traces WHERE source = 'replay'")
                            ).scalar_one() == 2
        assert conn.execute(text(
            "SELECT count(*) FROM issue_occurrences o JOIN traces t ON t.trace_id = o.trace_id "
            "WHERE t.source = 'replay'")).scalar_one() == 0
        events = conn.execute(text(
            "SELECT details ->> 'verdict' FROM issue_events WHERE kind = 'replayed' "
            "ORDER BY id")).scalars().all()
    assert events == ["failing", "passing"]
    # The trace list shows production traffic unless replays are asked for.
    sources = {name: [t["source"] for t in stack.api("GET", "/v1/traces", params=params
                                                     ).json()["items"]]
               for name, params in (("default", {}), ("replay", {"source": "replay"}),
                                    ("all", {"source": "all"}))}
    assert sources == {"default": ["production"], "replay": ["replay"] * 2,
                       "all": ["replay", "replay", "production"]}


def test_changed_input_diverges_instead_of_guessing(stack: Stack) -> None:
    record_production_run(stack)
    case_id = make_case(stack, "repeated_tool_call")
    stack.api("PATCH", f"/v1/regression-cases/{case_id}",
              json={"input": {"request": "upgrade", "account_id": "ent-unrecorded"}})
    result = stack.replay(case_id, "--purpose", "reproduce")
    assert result.returncode == 2 and "verdict: diverged" in result.stdout
    assert "no recorded result for 'get_account'" in latest_run(stack, case_id)["summary"]


def test_a_pass_without_prior_reproduction_proves_nothing(stack: Stack) -> None:
    record_production_run(stack)
    case_id = make_case(stack, "repeated_tool_call")
    result = stack.replay(case_id, "--purpose", "verify", fixed=True)
    assert result.returncode == 1
    run = latest_run(stack, case_id)
    assert run["verdict"] == "passing" and "never reproduced" in run["summary"]
    assert stack.api("GET", f"/v1/regression-cases/{case_id}").json()["status"] == "draft"


def test_failures_that_need_live_tools_are_inconclusive(stack: Stack) -> None:
    record_production_run(stack)
    case_id = make_case(stack, "timeout")
    result = stack.replay(case_id, "--purpose", "reproduce")
    assert result.returncode == 2 and "verdict: inconclusive" in result.stdout
    assert "needs live tools" in result.stdout


def test_bad_entrypoints_crash_cleanly(stack: Stack) -> None:
    record_production_run(stack)
    case_id = make_case(stack, "repeated_tool_call")
    stack.api("PATCH", f"/v1/regression-cases/{case_id}",
              json={"entrypoint": "no_such_package.agent:handle"})
    result = stack.replay(case_id, "--purpose", "reproduce")
    assert result.returncode == 2 and "verdict: crashed" in result.stdout
    assert "ModuleNotFoundError" in latest_run(stack, case_id)["summary"]


def test_scopes_tenants_and_preconditions(stack: Stack) -> None:
    record_production_run(stack)
    case_id = make_case(stack, "repeated_tool_call")
    tenant = stack.harness.tenant()  # same tenant (idempotent)
    no_replay_scope = stack.harness.key(tenant, "read", "ingest", "triage")
    assert stack.replay(case_id, "--purpose", "reproduce", key=no_replay_scope).returncode == 2
    other = stack.harness.key(stack.harness.tenant("globex"), "read", "replay", "ingest")
    denied = httpx.post(f"{stack.url}/v1/regression-cases/{case_id}/replays",
                        json={"purpose": "reproduce"},
                        headers={"authorization": f"Bearer {other}"})
    assert denied.status_code == 404
    stack.api("PATCH", f"/v1/regression-cases/{case_id}", json={"entrypoint": None})
    blocked = stack.api("POST", f"/v1/regression-cases/{case_id}/replays",
                        json={"purpose": "reproduce"})
    assert blocked.status_code == 422 and "entrypoint" in blocked.json()["error"]["message"]
    fake_run = stack.api("POST", f"/v1/replay-runs/{uuid.uuid4()}/report", json={})
    assert fake_run.status_code == 404
