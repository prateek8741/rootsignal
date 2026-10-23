from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.trace import ReadableSpan
from sqlalchemy import Engine, text

import rootsignal as rs
from conftest import PROTOBUF, TEST_DATABASE_URL, Harness, make_harness
from otlp_factory import TRACE_ID, raw_request, record_sdk_spans, request_from, sdk_request
from rootsignal_server import admin
from rootsignal_server.cli import main as cli_main
from rootsignal_server.config import get_settings


class UpstreamTimeout(Exception):
    pass


@rs.tool(side_effect="read")
def get_account(account_id: str) -> dict[str, str]:
    raise UpstreamTimeout(f"lookup of {account_id} timed out")


@rs.tool(side_effect="write")
def create_ticket(summary: str, contact: str) -> str:
    return "T-1"


def support_agent() -> str:
    with rs.agent_run("support-agent", agent_version="v1", max_steps=4) as run:
        with rs.llm_call("llama3.1:8b", provider="ollama",
                         input_messages=[{"role": "user", "content": "help"}]) as call:
            call.set_response([{"role": "assistant", "content": "checking"}],
                              input_tokens=30, output_tokens=5)
        for _ in range(3):
            with contextlib.suppress(UpstreamTimeout):
                get_account("ent-42")
        create_ticket("lookup failing", contact="jane@example.com")
        run.set_output("I've updated your account")
    return "done"


# ---------------------------------------------------------------------------
# End to end over real HTTP, with the real SDK exporter
# ---------------------------------------------------------------------------

def test_sdk_to_api_end_to_end(clean_db: Engine, live_server: str) -> None:
    h = make_harness(clean_db)
    tenant = h.tenant()
    ingest_key, read_key = h.key(tenant, "ingest"), h.key(tenant, "read")

    rs.init(rs.Config(api_key=ingest_key, endpoint=live_server, service_name="support"))
    try:
        assert support_agent() == "done"
        assert rs.flush(10)
        assert rs.stats().exported_spans == 6 and rs.stats().failed_spans == 0
    finally:
        rs.shutdown()

    listing = h.get("/v1/traces", read_key).json()
    assert len(listing["items"]) == 1
    summary = listing["items"][0]
    assert summary["agent_name"] == "support-agent" and summary["agent_version"] == "v1"
    assert summary["service_name"] == "support"
    assert (summary["span_count"], summary["error_count"], summary["llm_call_count"],
            summary["tool_call_count"]) == (6, 3, 1, 4)
    assert (summary["input_tokens"], summary["output_tokens"]) == (30, 5)
    assert summary["cost_usd"] == "0.000000" and summary["unpriced_llm_call_count"] == 0
    assert summary["status"] == "unset" and summary["completed_at"] is not None
    assert summary["started_at"].endswith(("Z", "+00:00"))

    detail = h.get(f"/v1/traces/{summary['trace_id']}", read_key).json()
    spans = detail["spans"]
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["kind"] == "agent" and root["span_id"] == summary["root_span_id"]
    assert all(s["parent_span_id"] == root["span_id"] for s in spans if s is not root)
    lookups = [s for s in spans if s["tool_name"] == "get_account"]
    assert [s["repeat_index"] for s in sorted(lookups, key=lambda s: s["call_index"])] == [0, 1, 2]
    assert {s["error_type"] for s in lookups} == {"UpstreamTimeout"}

    ticket = next(s for s in spans if s["tool_name"] == "create_ticket")
    assert ticket["side_effect"] == "write"
    args = h.get(f"/v1/payloads/{ticket['input_payload_hash']}", read_key).json()
    assert "jane@example.com" not in args["body"] and args["redaction_state"] == "sdk"

    assert h.scalar("SELECT count(*) FROM jobs WHERE kind = 'trace_completed'") == 1
    assert h.scalar("SELECT spans_accepted FROM usage_daily") == 6


# ---------------------------------------------------------------------------
# Authentication, authorisation, tenant isolation
# ---------------------------------------------------------------------------

def test_authentication_failures(harness: Harness) -> None:
    tenant = harness.tenant()
    key = harness.key(tenant)
    request = sdk_request(support_agent)

    missing = harness.post(request, None)
    assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
    assert harness.post(request, "rs_" + "x" * 43).status_code == 401
    assert harness.post(request, "not-a-key").status_code == 401

    with harness.engine.begin() as conn:
        admin.revoke_api_key(conn, key[:11], actor="test")
    assert harness.post(request, key).status_code == 401
    assert harness.scalar("SELECT count(*) FROM spans") == 0


def test_scopes_are_enforced(harness: Harness) -> None:
    tenant = harness.tenant()
    ingest_only, read_only = harness.key(tenant, "ingest"), harness.key(tenant, "read")
    assert harness.get("/v1/traces", ingest_only).status_code == 403
    assert harness.post(sdk_request(support_agent), read_only).status_code == 403


def test_tenants_and_environments_are_isolated(harness: Harness) -> None:
    a = harness.tenant("acme")
    b = harness.tenant("globex")
    a_staging = harness.tenant("acme", environment="staging")
    assert harness.post(sdk_request(support_agent), harness.key(a)).status_code == 200
    trace_id = harness.scalar("SELECT trace_id FROM traces")
    payload = harness.scalar("SELECT input_payload_hash FROM spans WHERE kind = 'tool' LIMIT 1")

    a_read = harness.key(a, "read")
    assert harness.get(f"/v1/traces/{trace_id}", a_read).status_code == 200
    for other in (harness.key(b, "read"), harness.key(a_staging, "read")):
        assert harness.get("/v1/traces", other).json()["items"] == []
        assert harness.get(f"/v1/traces/{trace_id}", other).status_code == 404
        assert harness.get(f"/v1/payloads/{payload}", other).status_code == 404


def test_same_trace_id_from_two_tenants_stays_separate(harness: Harness) -> None:
    a, b = harness.tenant("acme"), harness.tenant("globex")
    request = raw_request([{"span_id": b"\x01" * 8, "name": "root"}])
    assert harness.post(request, harness.key(a)).status_code == 200
    assert harness.post(request, harness.key(b)).status_code == 200
    assert harness.scalar("SELECT count(*) FROM traces WHERE trace_id = :t",
                          t=TRACE_ID.hex()) == 2


# ---------------------------------------------------------------------------
# Ingest semantics
# ---------------------------------------------------------------------------

def test_duplicate_batches_are_idempotent(harness: Harness) -> None:
    key = harness.key(harness.tenant())
    request = sdk_request(support_agent)
    for _ in range(2):
        assert harness.post(request, key).status_code == 200
    assert harness.scalar("SELECT count(*) FROM spans") == 6
    assert harness.scalar("SELECT span_count FROM traces") == 6
    assert harness.scalar("SELECT count(*) FROM jobs") == 1


def test_trace_completes_when_root_arrives_in_a_later_batch(harness: Harness) -> None:
    key = harness.key(harness.tenant())
    spans: Sequence[ReadableSpan] = record_sdk_spans(support_agent)
    children = [s for s in spans if s.parent is not None]
    roots = [s for s in spans if s.parent is None]

    assert harness.post(request_from(children), key).status_code == 200
    assert harness.scalar("SELECT completed_at IS NULL FROM traces") is True
    assert harness.scalar("SELECT count(*) FROM jobs") == 0

    assert harness.post(request_from(roots), key).status_code == 200
    assert harness.scalar("SELECT completed_at IS NOT NULL FROM traces") is True
    assert harness.scalar("SELECT span_count FROM traces") == 6
    assert harness.scalar("SELECT agent_name FROM traces") == "support-agent"
    assert harness.scalar("SELECT count(*) FROM jobs WHERE status = 'queued'") == 1


def test_oversized_payloads_are_truncated_with_marker(harness: Harness) -> None:
    tenant = harness.tenant()
    harness.set_project(tenant, max_payload_bytes=1024)

    def big() -> None:
        with rs.agent_run("a"), rs.tool_call("dump", side_effect="read") as t:
            t.set_result("x" * 5000)

    assert harness.post(sdk_request(big), harness.key(tenant)).status_code == 200
    with harness.engine.connect() as conn:
        body, size, truncated = conn.execute(text(
            "SELECT p.body, p.byte_size, p.truncated FROM payloads p "
            "JOIN spans s ON s.output_payload_hash = p.sha256 WHERE s.kind = 'tool'")).one()
    assert truncated and size <= 1024 and body.endswith("…[truncated]")


def test_partial_success_reports_rejected_spans(harness: Harness) -> None:
    key = harness.key(harness.tenant())
    request = raw_request([{"span_id": b"\x00" * 8}, {"span_id": b"\x01" * 8}])
    response = harness.post(request, key)
    assert response.status_code == 200
    parsed = ExportTraceServiceResponse.FromString(response.content)
    assert parsed.partial_success.rejected_spans == 1
    assert "invalid span_id" in parsed.partial_success.error_message
    assert harness.scalar("SELECT count(*) FROM spans") == 1


def test_json_and_gzip_requests(harness: Harness) -> None:
    key = harness.key(harness.tenant())
    assert harness.post(sdk_request(support_agent), key, compress=True).status_code == 200
    body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": "5b8efff798038103d269b633813fc60c", "spanId": "eee19b7ec3c1b174",
        "name": "json-span", "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000100000000"}]}]}]})
    response = harness.client.post("/v1/traces", content=body, headers={
        "content-type": "application/json", "authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert harness.scalar("SELECT count(*) FROM traces") == 2


def test_request_validation_errors(clean_db: Engine) -> None:
    h = make_harness(clean_db, max_request_bytes=2048)
    key = h.key(h.tenant())
    assert h.post(sdk_request(support_agent), key, content_type="text/plain").status_code == 415
    garbage = h.client.post("/v1/traces", content=b"\xff\xff\xff", headers={
        "content-type": PROTOBUF, "authorization": f"Bearer {key}"})
    assert garbage.status_code == 400
    assert garbage.json()["error"]["code"] == "invalid_request"

    def huge() -> None:
        with rs.agent_run("a"), rs.tool_call("dump", side_effect="read") as t:
            t.set_result("y" * 10_000)

    too_big = h.post(sdk_request(huge), key)
    assert too_big.status_code == 413
    assert h.scalar("SELECT count(*) FROM spans") == 0


def test_daily_span_quota(harness: Harness) -> None:
    tenant = harness.tenant()
    harness.set_project(tenant, max_spans_per_day=8)
    key = harness.key(tenant)
    assert harness.post(sdk_request(support_agent), key).status_code == 200  # 6 spans
    rejected = harness.post(sdk_request(support_agent), key)  # would reach 12
    assert rejected.status_code == 429
    assert 0 < int(rejected.headers["retry-after"]) <= 86_400
    assert rejected.json()["error"]["code"] == "quota_exceeded"
    assert harness.scalar("SELECT count(*) FROM spans") == 6  # nothing from the rejected batch
    assert harness.scalar("SELECT rejected_requests FROM usage_daily") == 1
    assert harness.scalar("SELECT spans_accepted FROM usage_daily") == 6


def test_rate_limit_per_key(clean_db: Engine) -> None:
    h = make_harness(clean_db, rate_limit_per_second=0.01, rate_limit_burst=2)
    tenant = h.tenant()
    key, other = h.key(tenant, "read"), h.key(tenant, "read")
    assert [h.get("/v1/traces", key).status_code for _ in range(3)] == [200, 200, 429]
    assert h.get("/v1/traces", other).status_code == 200


def test_pagination_and_filters(harness: Harness) -> None:
    tenant = harness.tenant()
    key, read = harness.key(tenant), harness.key(tenant, "read")
    for i in range(5):
        trace_id = bytes([i + 1]) * 16
        harness.post(raw_request([{"span_id": b"\x01" * 8, "trace_id": trace_id,
                                   "start": 1_700_000_000_000_000_000 + i * 1_000_000_000,
                                   "status": 2 if i % 2 else 0}]), key)
    first = harness.get("/v1/traces?limit=2", read).json()
    second = harness.get(f"/v1/traces?limit=2&cursor={first['next_cursor']}", read).json()
    third = harness.get(f"/v1/traces?limit=2&cursor={second['next_cursor']}", read).json()
    seen = [t["trace_id"] for page in (first, second, third) for t in page["items"]]
    assert len(seen) == len(set(seen)) == 5 and third["next_cursor"] is None
    errors = harness.get("/v1/traces?status=error", read).json()["items"]
    assert len(errors) == 2 and {t["status"] for t in errors} == {"error"}
    assert harness.get("/v1/traces?cursor=garbage", read).status_code == 400
    assert harness.get("/v1/traces/not-a-trace-id", read).status_code == 422


def test_health_endpoints(harness: Harness) -> None:
    assert harness.client.get("/healthz").json() == {"status": "ok"}
    assert harness.client.get("/readyz").status_code == 200


def test_cli_creates_tenant_and_key_with_audit_trail(
        clean_db: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ROOTSIGNAL_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    try:
        assert cli_main(["create-tenant", "--org", "acme", "--project", "support"]) == 0
        assert cli_main(["create-key", "--org", "acme", "--project", "support",
                         "--name", "ci", "--scopes", "ingest,read"]) == 0
        key = capsys.readouterr().out.strip().splitlines()[-1]
        assert key.startswith("rs_")
        assert cli_main(["create-key", "--org", "acme", "--project", "missing",
                         "--name", "x"]) == 1
        assert cli_main(["revoke-key", key[:11]]) == 0
    finally:
        get_settings.cache_clear()

    h = make_harness(clean_db)
    assert h.get("/v1/traces", key).status_code == 401  # revoked
    with clean_db.connect() as conn:
        actions = conn.execute(text(
            "SELECT action FROM audit_events ORDER BY id")).scalars().all()
        stored = conn.execute(text("SELECT key_hash FROM api_keys")).scalar_one()
    assert actions == ["tenant.ensure", "api_key.create", "api_key.revoke"]
    assert key not in stored  # only the hash is persisted
