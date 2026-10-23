from __future__ import annotations

import contextlib
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

import rootsignal as rs
from conftest import TEST_DATABASE_URL, Harness
from otlp_factory import record_sdk_spans, request_from, sdk_request
from rootsignal_server.cli import main as cli_main
from rootsignal_server.config import get_settings
from rootsignal_server.jobs import queue
from rootsignal_server.jobs.queue import ClaimedJob
from rootsignal_server.jobs.worker import Worker, default_handlers


class UpstreamTimeout(Exception):
    pass


def llm() -> None:
    with rs.llm_call("llama3.1:8b", provider="ollama") as c:
        c.set_response(input_tokens=50, output_tokens=10)


def retry_loop_agent() -> None:
    """The slice's seeded bug: identical failing lookup until the step limit."""
    with rs.agent_run("support-agent", agent_version="v1", max_steps=4) as run:
        for _ in range(4):
            llm()
            with contextlib.suppress(UpstreamTimeout), rs.tool_call(
                    "get_account", arguments={"account_id": "ent-42"}, side_effect="read"):
                raise UpstreamTimeout("account service timed out")
        run.set_output("I've updated your account.")


def worker(engine: Engine, **kwargs: Any) -> Worker:
    defaults: dict[str, Any] = {"maintenance_interval_s": 0.0,
                                "trace_idle_timeout": timedelta(seconds=60)}
    return Worker(engine, kwargs.pop("handlers", default_handlers()), **{**defaults, **kwargs})


def detectors_for(h: Harness, trace_id: str | None = None) -> set[str]:
    with h.engine.connect() as conn:
        sql = "SELECT detector FROM findings" + (" WHERE trace_id = :t" if trace_id else "")
        return set(conn.execute(text(sql), {"t": trace_id}).scalars())


def job_statuses(h: Harness) -> list[str]:
    with h.engine.connect() as conn:
        return list(conn.execute(text("SELECT status FROM jobs ORDER BY id")).scalars())


def test_completed_trace_is_detected_and_exposed_in_the_api(harness: Harness) -> None:
    tenant = harness.tenant()
    assert harness.post(sdk_request(retry_loop_agent), harness.key(tenant)).status_code == 200
    w = worker(harness.engine)
    assert w.run_once() == 1
    assert w.stats.done == 1 and job_statuses(harness) == ["done"]
    assert detectors_for(harness) == {"timeout", "repeated_tool_call", "excessive_retries",
                                      "step_limit_reached"}

    trace_id = harness.scalar("SELECT trace_id FROM traces")
    detail = harness.get(f"/v1/traces/{trace_id}", harness.key(tenant, "read")).json()
    findings = detail["findings"]
    assert [f["severity"] for f in findings] == sorted(
        (f["severity"] for f in findings), key=["critical", "high", "medium", "low"].index)
    span_ids = {s["span_id"] for s in detail["spans"]}
    for finding in findings:
        assert set(finding["evidence_span_ids"]) <= span_ids  # evidence points at real spans
    loop = next(f for f in findings if f["detector"] == "repeated_tool_call")
    assert loop["tool_name"] == "get_account" and loop["facts"]["calls"] == 4
    assert detail["trace"]["completion_reason"] == "root_span"


def test_redetection_is_idempotent_and_keeps_finding_ids(harness: Harness) -> None:
    tenant = harness.tenant()
    request = sdk_request(retry_loop_agent)
    harness.post(request, harness.key(tenant))
    worker(harness.engine).run_once()
    with harness.engine.connect() as conn:
        before = dict(conn.execute(text("SELECT detector, id FROM findings")).all())
    harness.post(request, harness.key(tenant))  # duplicate batch re-enqueues detection
    assert job_statuses(harness) == ["done", "queued"]
    worker(harness.engine).run_once()
    with harness.engine.connect() as conn:
        after = dict(conn.execute(text("SELECT detector, id FROM findings")).all())
    assert after == before


def test_idle_trace_is_completed_and_late_root_triggers_redetection(harness: Harness) -> None:
    tenant = harness.tenant()
    key = harness.key(tenant)
    spans = record_sdk_spans(retry_loop_agent)
    children = [s for s in spans if s.parent is not None]
    first, second = children[:3], children[3:]
    roots = [s for s in spans if s.parent is None]

    harness.post(request_from(first), key)
    w = worker(harness.engine, trace_idle_timeout=timedelta(0))
    w.run_once()  # maintenance completes the idle trace, then detection runs
    assert harness.scalar("SELECT completion_reason FROM traces") == "idle_timeout"
    completed_at = harness.scalar("SELECT completed_at FROM traces")
    assert "step_limit_reached" not in detectors_for(harness)  # agent span not seen yet

    harness.post(request_from(second), key)  # late children, still no root
    assert harness.scalar("SELECT completed_at FROM traces") == completed_at
    assert harness.scalar("SELECT completion_reason FROM traces") == "idle_timeout"

    harness.post(request_from(roots), key)  # root finally arrives
    assert harness.scalar("SELECT completion_reason FROM traces") == "root_span"
    assert harness.scalar("SELECT completed_at FROM traces") == completed_at
    while w.run_once():
        pass
    assert "step_limit_reached" in detectors_for(harness)


def test_failing_handler_retries_with_backoff_then_goes_dead(harness: Harness) -> None:
    tenant = harness.tenant()
    harness.post(sdk_request(retry_loop_agent), harness.key(tenant))
    with harness.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET max_attempts = 2"))

    def explode(conn: Connection, job: ClaimedJob) -> None:
        conn.execute(text("UPDATE traces SET agent_name = 'tampered'"))  # must roll back
        raise RuntimeError("handler bug")

    w = worker(harness.engine, handlers={"trace_completed": explode})
    w.run_once()
    assert job_statuses(harness) == ["queued"] and w.stats.retried == 1
    assert harness.scalar("SELECT run_after > now() FROM jobs") is True  # backoff applied
    assert harness.scalar("SELECT agent_name FROM traces") == "support-agent"

    with harness.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET run_after = now()"))
    w.run_once()
    assert job_statuses(harness) == ["dead"] and w.stats.dead == 1
    assert "RuntimeError: handler bug" in harness.scalar("SELECT last_error FROM jobs")


def test_unknown_job_kind_is_dead_lettered(harness: Harness) -> None:
    tenant = harness.tenant()
    with harness.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO jobs (org_id, project_id, kind, max_attempts) "
            "VALUES (:o, :p, 'mystery', 1)"), {"o": tenant.org_id, "p": tenant.project_id})
    worker(harness.engine).run_once()
    assert job_statuses(harness) == ["dead"]
    assert "no handler" in harness.scalar("SELECT last_error FROM jobs")


def test_stale_running_jobs_are_requeued(harness: Harness) -> None:
    tenant = harness.tenant()
    harness.post(sdk_request(retry_loop_agent), harness.key(tenant))
    with harness.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'running', locked_by = 'dead-worker', "
                          "locked_at = now() - interval '1 hour', attempts = 1"))
    w = worker(harness.engine, visibility_timeout=timedelta(minutes=5))
    w.run_once()  # maintenance requeues it, then this worker processes it
    assert job_statuses(harness) == ["done"]
    assert detectors_for(harness)


def test_lost_lock_rolls_back_handler_effects(harness: Harness) -> None:
    tenant = harness.tenant()
    harness.post(sdk_request(retry_loop_agent), harness.key(tenant))

    def slow_handler(conn: Connection, job: ClaimedJob) -> None:
        conn.execute(text("UPDATE traces SET agent_name = 'from-stale-worker'"))
        with harness.engine.begin() as other:  # meanwhile another worker took the job over
            other.execute(text("UPDATE jobs SET locked_by = 'someone-else'"))

    w = worker(harness.engine, handlers={"trace_completed": slow_handler})
    w.run_once()
    assert w.stats.lost_lock == 1 and w.stats.done == 0
    assert harness.scalar("SELECT agent_name FROM traces") == "support-agent"


def test_concurrent_claims_never_overlap(harness: Harness) -> None:
    tenant = harness.tenant()
    with harness.engine.begin() as conn:
        for i in range(20):
            conn.execute(text("INSERT INTO jobs (org_id, project_id, kind, dedupe_key) "
                              "VALUES (:o, :p, 'trace_completed', :k)"),
                         {"o": tenant.org_id, "p": tenant.project_id, "k": f"k{i}"})
    with harness.engine.connect() as a, harness.engine.connect() as b:
        ta, tb = a.begin(), b.begin()
        first = queue.claim(a, "worker-a", limit=12)  # rows stay locked: tx still open
        second = queue.claim(b, "worker-b", limit=20)
        ta.commit()
        tb.commit()
    ids_a, ids_b = {j.id for j in first}, {j.id for j in second}
    assert len(ids_a) == 12 and len(ids_b) == 8 and not ids_a & ids_b


def test_project_overrides_and_invalid_config(harness: Harness,
                                              caplog: pytest.LogCaptureFixture) -> None:
    tenant = harness.tenant()
    harness.set_project(tenant, detector_config='{"repeated_tool_call": {"enabled": false}}')
    harness.post(sdk_request(retry_loop_agent), harness.key(tenant))
    worker(harness.engine).run_once()
    assert "repeated_tool_call" not in detectors_for(harness)

    other = harness.tenant("globex")
    harness.set_project(other, detector_config='{"repeated_tool_call": {"enabled": "nope"}}')
    harness.post(sdk_request(retry_loop_agent), harness.key(other))
    with caplog.at_level(logging.ERROR, logger="rootsignal.detection"):
        worker(harness.engine).run_once()
    assert "invalid detector_config" in caplog.text
    with harness.engine.connect() as conn:
        globex = set(conn.execute(text(
            "SELECT detector FROM findings WHERE project_id = :p"),
            {"p": other.project_id}).scalars())
    assert "repeated_tool_call" in globex  # bad config falls back to defaults, not to "off"


def test_cli_worker_once(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = harness.tenant()
    harness.post(sdk_request(retry_loop_agent), harness.key(tenant))
    monkeypatch.setenv("ROOTSIGNAL_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    try:
        assert cli_main(["worker", "--once"]) == 0
    finally:
        get_settings.cache_clear()
    assert job_statuses(harness) == ["done"] and detectors_for(harness)


def test_redetection_removes_findings_that_no_longer_apply(harness: Harness) -> None:
    tenant = harness.tenant()
    request = sdk_request(retry_loop_agent)
    harness.post(request, harness.key(tenant))
    worker(harness.engine).run_once()
    with harness.engine.connect() as conn:
        before = dict(conn.execute(text("SELECT detector, id FROM findings")).all())
    assert "repeated_tool_call" in before

    harness.set_project(tenant, detector_config='{"repeated_tool_call": {"enabled": false}}')
    harness.post(request, harness.key(tenant))  # re-enqueue detection
    worker(harness.engine).run_once()
    with harness.engine.connect() as conn:
        after = dict(conn.execute(text("SELECT detector, id FROM findings")).all())
    assert after == {d: i for d, i in before.items() if d != "repeated_tool_call"}

    everything_off = {name: {"enabled": False} for name in before}
    harness.set_project(tenant, detector_config=json.dumps(everything_off))
    harness.post(request, harness.key(tenant))
    worker(harness.engine).run_once()
    assert detectors_for(harness) == set()
