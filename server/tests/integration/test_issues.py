from __future__ import annotations

import contextlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

import rootsignal as rs
from conftest import TEST_DATABASE_URL, Harness
from otlp_factory import sdk_request
from rootsignal_server.admin import TenantIds
from rootsignal_server.cli import main as cli_main
from rootsignal_server.config import get_settings
from rootsignal_server.issues.grouping import group_trace
from rootsignal_server.jobs.worker import Worker, default_handlers


class UpstreamError(Exception):
    pass


def failing_lookup(account: str, version: str = "v1", tool: str = "get_account",
                   error: type[Exception] = UpstreamError,
                   agent: str = "support-agent") -> Callable[[], None]:
    def build() -> None:
        with rs.agent_run(agent, agent_version=version):
            with rs.llm_call("m", provider="ollama") as c:
                c.set_response(input_tokens=5, output_tokens=1)
            with contextlib.suppress(error), rs.tool_call(
                    tool, arguments={"account_id": account}, side_effect="read"):
                raise error("upstream failed")
    return build


def looping_search(calls: int, version: str = "v1") -> Callable[[], None]:
    def build() -> None:
        with rs.agent_run("docs-agent", agent_version=version):
            for _ in range(calls):
                with rs.tool_call("search_docs", arguments={"q": "E42"}, side_effect="none"):
                    pass
    return build


def ingest(h: Harness, key: str, build: Callable[[], None]) -> str:
    request = sdk_request(build)
    assert h.post(request, key).status_code == 200
    process(h)
    trace_id = request.resource_spans[0].scope_spans[0].spans[0].trace_id.hex()
    return trace_id


def process(h: Harness) -> None:
    w = Worker(h.engine, default_handlers(), maintenance_interval_s=3600,
               trace_idle_timeout=timedelta(hours=1))
    while w.run_once():
        pass
    assert w.stats.dead == 0 and w.stats.retried == 0, w.stats


def issues(h: Harness, **where: Any) -> list[dict[str, Any]]:
    clause = " AND ".join(f"{k} = :{k}" for k in where) or "true"
    with h.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            f"SELECT * FROM issues WHERE {clause} ORDER BY created_at"), where).mappings()]


def events(h: Harness, issue_id: uuid.UUID) -> list[tuple[str, str]]:
    with h.engine.connect() as conn:
        return [(r.kind, r.actor) for r in conn.execute(text(
            "SELECT kind, actor FROM issue_events WHERE issue_id = :i ORDER BY id"),
            {"i": issue_id})]


def set_status(h: Harness, key: str, issue_id: uuid.UUID, status: str,
               **body: Any) -> Any:
    return h.client.post(f"/v1/issues/{issue_id}/status", json={"status": status, **body},
                         headers={"authorization": f"Bearer {key}"})


@pytest.fixture
def setup(harness: Harness) -> tuple[Harness, TenantIds, str, str]:
    tenant = harness.tenant()
    return harness, tenant, harness.key(tenant, "ingest"), harness.key(tenant, "read", "triage")


# --- grouping ------------------------------------------------------------------

def test_ten_identical_failures_make_one_issue(setup: Any) -> None:
    h, _, key, _ = setup
    traces = [ingest(h, key, failing_lookup(f"acc-{i}")) for i in range(10)]
    [issue] = issues(h, detector="tool_error")
    assert issue["occurrence_count"] == 10
    assert issue["title"] == "'get_account' fails with UpstreamError in support-agent"
    assert issue["status"] == "open" and issue["severity"] == "medium"
    assert (issue["first_trace_id"], issue["latest_trace_id"]) == (traces[0], traces[-1])
    assert issue["first_seen_at"] < issue["last_seen_at"]
    assert issue["affected_versions"] == ["v1"]


def test_different_tool_or_agent_is_a_separate_issue(setup: Any) -> None:
    h, _, key, _ = setup
    ingest(h, key, failing_lookup("a"))
    ingest(h, key, failing_lookup("a", tool="get_invoice"))
    ingest(h, key, failing_lookup("a", agent="billing-agent"))
    ingest(h, key, failing_lookup("b"))
    counts = sorted(i["occurrence_count"] for i in issues(h, detector="tool_error"))
    assert counts == [1, 1, 2]


def test_environments_have_separate_issues(harness: Harness) -> None:
    prod = harness.tenant(environment="production")
    staging = harness.tenant(environment="staging")
    ingest(harness, harness.key(prod), failing_lookup("a"))
    ingest(harness, harness.key(staging), failing_lookup("a"))
    assert len(issues(harness, detector="tool_error")) == 2
    staging_view = harness.get("/v1/issues", harness.key(staging, "read")).json()["items"]
    prod_issue = issues(harness, detector="tool_error", environment_id=prod.environment_id)[0]
    assert prod_issue["id"] not in {i["id"] for i in staging_view}
    assert harness.get(f"/v1/issues/{prod_issue['id']}",
                       harness.key(staging, "read")).status_code == 404


def test_representative_traces_and_versions(setup: Any) -> None:
    h, _, key, read = setup
    small = ingest(h, key, looping_search(3, version="v1"))
    worst = ingest(h, key, looping_search(6, version="v2"))
    latest = ingest(h, key, looping_search(4, version="v2"))
    [issue] = issues(h, detector="repeated_tool_call")
    assert (issue["first_trace_id"], issue["worst_trace_id"], issue["latest_trace_id"]) == \
        (small, worst, latest)
    assert issue["affected_versions"] == ["v1", "v2"]
    detail = h.get(f"/v1/issues/{issue['id']}", read).json()
    assert {v["agent_version"]: v["occurrences"] for v in detail["versions"]} == \
        {"v1": 1, "v2": 2}
    assert [o["trace_id"] for o in detail["recent_occurrences"]] == [latest, worst, small]


def test_replay_traces_are_not_grouped(setup: Any) -> None:
    h, _, key, _ = setup
    request = sdk_request(failing_lookup("a"))
    h.post(request, key)
    with h.engine.begin() as conn:
        conn.execute(text("UPDATE traces SET source = 'replay'"))
    process(h)
    assert h.scalar("SELECT count(*) FROM findings") > 0
    assert issues(h) == []


def test_removed_findings_decrement_counts(setup: Any) -> None:
    h, tenant, key, _ = setup
    first = sdk_request(failing_lookup("a"))
    h.post(first, key)
    ingest(h, key, failing_lookup("b"))
    assert issues(h, detector="tool_error")[0]["occurrence_count"] == 2
    h.set_project(tenant, detector_config='{"tool_error": {"enabled": false}}')
    h.post(first, key)  # duplicate batch re-runs detection for the first trace
    process(h)
    assert issues(h, detector="tool_error")[0]["occurrence_count"] == 1


# --- lifecycle -----------------------------------------------------------------

def test_resolved_issue_reopens_as_regression(setup: Any) -> None:
    h, _, key, triage = setup
    ingest(h, key, failing_lookup("a", version="v1"))
    issue_id = issues(h, detector="tool_error")[0]["id"]
    response = set_status(h, triage, issue_id, "resolved", note="fixed the client timeout")
    assert response.status_code == 200 and response.json()["status"] == "resolved"

    ingest(h, key, failing_lookup("b", version="v2"))
    [issue] = issues(h, detector="tool_error")
    assert issue["status"] == "open" and issue["is_regression"]
    assert issue["regression_count"] == 1 and issue["resolved_at"] is None
    prefix = triage[:11]
    assert events(h, issue_id) == [("created", "system"),
                                   ("status_changed", f"api_key:{prefix}"),
                                   ("regressed", "system")]


def test_stragglers_from_old_versions_do_not_reopen(setup: Any) -> None:
    h, _, key, triage = setup
    ingest(h, key, failing_lookup("a", version="v1"))
    issue_id = issues(h, detector="tool_error")[0]["id"]
    assert set_status(h, triage, issue_id, "resolved",
                      fixed_in_version="v2").json()["fixed_in_version"] == "v2"

    ingest(h, key, failing_lookup("b", version="v1"))  # old deploy still draining
    ingest(h, key, failing_lookup("c", version="v1"))
    [issue] = issues(h, detector="tool_error")
    assert issue["status"] == "resolved" and issue["occurrence_count"] == 3
    assert [k for k, _ in events(h, issue_id)].count("straggler") == 1

    ingest(h, key, failing_lookup("d", version="v2"))  # the fixed version fails too
    assert issues(h, detector="tool_error")[0]["status"] == "open"


def test_ignored_issues_stay_ignored(setup: Any) -> None:
    h, _, key, triage = setup
    ingest(h, key, failing_lookup("a"))
    issue_id = issues(h, detector="tool_error")[0]["id"]
    set_status(h, triage, issue_id, "ignored")
    ingest(h, key, failing_lookup("b", version="v9"))
    [issue] = issues(h, detector="tool_error")
    assert issue["status"] == "ignored" and issue["occurrence_count"] == 2


def test_runs_from_before_the_resolution_do_not_reopen(setup: Any) -> None:
    h, _, key, triage = setup
    ingest(h, key, failing_lookup("a"))
    issue_id = issues(h, detector="tool_error")[0]["id"]
    set_status(h, triage, issue_id, "resolved")
    with h.engine.begin() as conn:  # pretend the next run started before the fix landed
        conn.execute(text("UPDATE issues SET resolved_at = now() + interval '1 hour'"))
    ingest(h, key, failing_lookup("b"))
    assert issues(h, detector="tool_error")[0]["status"] == "resolved"


def test_concurrent_grouping_into_one_issue_loses_nothing(setup: Any) -> None:
    h, tenant, key, _ = setup
    trace_ids = []
    for account in ("a", "b"):
        request = sdk_request(failing_lookup(account))
        h.post(request, key)
        trace_ids.append(request.resource_spans[0].scope_spans[0].spans[0].trace_id.hex())
    process(h)
    with h.engine.begin() as conn:  # start over: only findings, no issues
        conn.execute(text("DELETE FROM issues"))

    first_holding = threading.Event()
    errors: list[BaseException] = []

    def second() -> None:
        try:
            first_holding.wait(5)
            with h.engine.begin() as conn:
                group_trace(conn, tenant.project_id, trace_ids[1])  # blocks on the lock
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=second)
    thread.start()
    with h.engine.begin() as conn:
        group_trace(conn, tenant.project_id, trace_ids[0])
        first_holding.set()
        thread.join(0.5)
        assert thread.is_alive()  # waiting for our advisory lock
    thread.join(10)
    assert not errors
    [issue] = issues(h, detector="tool_error")
    assert issue["occurrence_count"] == 2


# --- API -----------------------------------------------------------------------

def test_issue_api_listing_filters_sorting_and_paging(setup: Any) -> None:
    h, _, key, read = setup
    for i in range(3):
        ingest(h, key, failing_lookup(f"a{i}"))
    ingest(h, key, failing_lookup("x", tool="get_invoice"))
    ingest(h, key, looping_search(3))

    def get(query: str) -> Any:
        response = h.get(f"/v1/issues{query}", read)
        assert response.status_code == 200, response.text
        return response.json()

    by_count = get("?sort=occurrences")["items"]
    assert by_count[0]["occurrence_count"] == 3
    assert get("?detector=tool_error&sort=severity")["items"][0]["detector"] == "tool_error"
    pages, cursor = [], ""
    while True:
        page = get(f"?limit=1{cursor}")
        pages += page["items"]
        if not page["next_cursor"]:
            break
        cursor = f"&cursor={page['next_cursor']}"
    assert len(pages) == len({p["id"] for p in pages}) == len(get("")["items"])
    assert get("?status=resolved")["items"] == []
    assert h.get("/v1/issues?cursor=bogus", read).status_code == 400
    assert h.get("/v1/issues/not-a-uuid", read).status_code == 422

    trace = get("")["items"][0]["latest_trace_id"]
    findings = h.get(f"/v1/traces/{trace}", read).json()["findings"]
    assert all(f["issue_id"] for f in findings)


def test_status_changes_need_triage_scope_and_valid_transitions(setup: Any) -> None:
    h, tenant, key, triage = setup
    ingest(h, key, failing_lookup("a"))
    issue_id = issues(h, detector="tool_error")[0]["id"]
    read_only = h.key(tenant, "read")
    assert set_status(h, read_only, issue_id, "resolved").status_code == 403
    assert set_status(h, triage, issue_id, "open").status_code == 409  # already open
    assert set_status(h, triage, issue_id, "ignored",
                      fixed_in_version="v2").status_code == 409
    assert set_status(h, triage, issue_id, "closed").status_code == 422
    assert set_status(h, triage, uuid.uuid4(), "resolved").status_code == 404

    other = h.tenant("globex")
    assert set_status(h, h.key(other, "triage"), issue_id, "resolved").status_code == 404
    occurrences = h.get(f"/v1/issues/{issue_id}/occurrences", h.key(other, "read"))
    assert occurrences.status_code == 404
    assert issues(h, detector="tool_error")[0]["status"] == "open"


def test_regroup_cli_applies_new_grouping(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    h, tenant, key, _ = setup
    ingest(h, key, failing_lookup("a", error=UpstreamError))
    ingest(h, key, failing_lookup("b", error=TimeoutError))  # timeout, not tool_error
    ingest(h, key, failing_lookup("c", error=PermissionError))
    assert len(issues(h, detector="tool_error")) == 2
    h.set_project(tenant, issue_grouping=json.dumps(
        {"tool_error": ["detector", "agent_name", "tool_name"]}))
    monkeypatch.setenv("ROOTSIGNAL_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    try:
        assert cli_main(["regroup-issues", "--org", "acme", "--project", "support"]) == 0
    finally:
        get_settings.cache_clear()
    counts = sorted(i["occurrence_count"] for i in issues(h, detector="tool_error"))
    assert counts == [0, 0, 2]  # old per-error-type issues emptied, one merged issue


def test_invalid_grouping_config_falls_back_to_defaults(
        setup: Any, caplog: pytest.LogCaptureFixture) -> None:
    h, tenant, key, _ = setup
    h.set_project(tenant, issue_grouping='{"tool_error": ["tool_name"]}')
    with caplog.at_level(logging.ERROR, logger="rootsignal.issues"):
        ingest(h, key, failing_lookup("a"))
    assert "invalid issue_grouping" in caplog.text
    assert issues(h, detector="tool_error")[0]["components"]["error_type"] == "UpstreamError"
