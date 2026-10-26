from __future__ import annotations

import contextlib
import json
from decimal import Decimal

import pytest

import rootsignal as rs
from otlp_factory import raw_request, sdk_request
from rootsignal_server.ingest.normalize import IngestPolicy, NormalizedSpan, normalize
from rootsignal_server.ingest.pricing import ModelPrice, PriceTable


class UpstreamTimeout(Exception):
    pass


@rs.tool(side_effect="read")
def get_account(account_id: str) -> dict[str, str]:
    raise UpstreamTimeout(f"timeout for {account_id} (owner jane@example.com)")


@rs.tool("create_ticket", side_effect="write")
def create_ticket(summary: str) -> str:
    return "T-1 for bob@example.com"


def support_agent() -> None:
    with rs.agent_run("support-agent", agent_version="v1", prompt_version="p2",
                      conversation_id="c-1", max_steps=4):
        with rs.llm_call("llama3.1:8b", provider="ollama",
                         input_messages=[{"role": "system", "content": "be brief"},
                                         {"role": "user", "content": "help"}]) as call:
            call.set_response([{"role": "assistant", "content": "checking"}],
                              input_tokens=40, output_tokens=8)
        with rs.retrieval("refund policy", data_source="docs", top_k=2) as r:
            r.set_documents([{"id": "d1", "score": 0.9, "content": "30 days"}])
        for _ in range(2):
            with contextlib.suppress(UpstreamTimeout):
                get_account("ent-42")
        create_ticket("escalate")


def by_kind(spans: list[NormalizedSpan], kind: str) -> list[NormalizedSpan]:
    return [s for s in spans if s.kind == kind]


def test_sdk_spans_normalise_to_canonical_rows() -> None:
    batch = normalize(sdk_request(support_agent, service_name="support"), IngestPolicy())
    assert batch.rejected == 0
    spans = batch.spans
    assert sorted(s.kind for s in spans) == ["agent", "llm", "retrieval", "tool", "tool", "tool"]
    assert {s.dialect for s in spans} == {"rootsignal"}
    assert {s.trace_id for s in spans} == {spans[0].trace_id}

    agent = by_kind(spans, "agent")[0]
    assert agent.parent_span_id is None
    assert agent.service_name == "support"
    assert agent.attributes["gen_ai.agent.version"] == "v1"
    assert agent.input_payload is None  # user input not captured by default
    for child in spans:
        if child is not agent:
            assert child.parent_span_id == agent.span_id

    llm = by_kind(spans, "llm")[0]
    assert (llm.model, llm.provider, llm.input_tokens, llm.output_tokens) == \
        ("llama3.1:8b", "ollama", 40, 8)
    assert llm.cost_usd == Decimal(0)  # local provider: zero API cost
    assert llm.input_payload is not None and llm.input_payload.content_type == "json"
    assert "gen_ai.input.messages" not in llm.attributes  # moved to the payload table
    messages = json.loads(llm.input_payload.body)
    assert messages[1]["content"] == "[not captured]"

    lookups = [s for s in by_kind(spans, "tool") if s.tool_name == "get_account"]
    assert [s.repeat_index for s in sorted(lookups, key=lambda s: s.call_index or 0)] == [0, 1]
    assert all(s.status == "error" and s.error_type == "UpstreamTimeout" for s in lookups)
    assert all(s.side_effect == "read" and s.args_hash for s in lookups)
    assert lookups[0].args_hash == lookups[1].args_hash
    assert "jane@example.com" not in json.dumps(lookups[0].events)

    ticket = next(s for s in spans if s.tool_name == "create_ticket")
    assert ticket.side_effect == "write"
    assert ticket.output_payload is not None
    assert "bob@example.com" not in ticket.output_payload.body
    assert ticket.output_payload.redaction_state == "sdk"

    retrieval = by_kind(spans, "retrieval")[0]
    assert retrieval.input_payload is not None and retrieval.input_payload.body == "refund policy"
    assert retrieval.output_payload is not None
    assert json.loads(retrieval.output_payload.body)[0]["id"] == "d1"


def test_server_redaction_follows_policy() -> None:
    def leak() -> None:
        with rs.agent_run("a"):
            create_ticket("x")

    # SDK redaction off: the payload arrives raw and is marked accordingly.
    unredacted = sdk_request(leak, redact_pii=False)
    ticket = next(s for s in normalize(unredacted, IngestPolicy(redact_on_ingest="never")).spans
                  if s.kind == "tool")
    assert ticket.output_payload is not None
    assert "bob@example.com" in ticket.output_payload.body
    assert ticket.output_payload.redaction_state == "none"

    for policy in ("if_not_sdk", "always"):
        ticket = next(s for s in normalize(unredacted, IngestPolicy(redact_on_ingest=policy)).spans
                      if s.kind == "tool")
        assert ticket.output_payload is not None
        assert "bob@example.com" not in ticket.output_payload.body
        assert ticket.output_payload.redaction_state == "server"


def test_unknown_sources_are_redacted_server_side_including_attributes() -> None:
    request = raw_request([{"span_id": b"\x01" * 8, "attributes": {
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup",
        "gen_ai.tool.call.arguments": '{"email": "eve@example.com"}',
        "user.contact": "call (415) 555-0134"}}])
    span = normalize(request, IngestPolicy()).spans[0]
    assert span.dialect == "gen_ai" and span.kind == "tool"
    assert span.side_effect == "unknown"  # undeclared: treated as side-effecting in replay
    assert span.input_payload is not None
    assert span.input_payload.redaction_state == "server"
    assert "eve@example.com" not in span.input_payload.body
    assert "555-0134" not in span.attributes["user.contact"]


def test_payloads_are_capped_server_side() -> None:
    def big() -> None:
        with rs.agent_run("a"), rs.tool_call("dump", side_effect="read") as t:
            t.set_result("x" * 5000)

    span = next(s for s in normalize(sdk_request(big), IngestPolicy(max_payload_bytes=1024)).spans
                if s.kind == "tool")
    assert span.output_payload is not None
    assert span.output_payload.truncated
    assert span.output_payload.byte_size <= 1024
    assert span.output_payload.body.endswith("…[truncated]")
    assert span.output_payload.content_type == "text"


def test_sdk_truncation_flag_is_preserved() -> None:
    def big() -> None:
        with rs.agent_run("a"), rs.tool_call("dump", side_effect="read") as t:
            t.set_result("y" * 2000)

    span = next(s for s in normalize(sdk_request(big, max_payload_bytes=512), IngestPolicy()).spans
                if s.kind == "tool")
    assert span.output_payload is not None and span.output_payload.truncated


def test_invalid_spans_are_rejected_with_reasons() -> None:
    request = raw_request([
        {"span_id": b"\x00" * 8},
        {"span_id": b"\x02" * 8, "trace_id": b"\x01" * 4},
        {"span_id": b"\x03" * 8, "start": 0},
        {"span_id": b"\x04" * 8},
    ])
    batch = normalize(request, IngestPolicy())
    assert len(batch.spans) == 1 and batch.rejected == 3
    assert batch.rejection_reasons == {"invalid span_id": 1, "invalid trace_id": 1,
                                       "missing start time": 1}
    assert "invalid span_id" in batch.error_message()


@pytest.mark.parametrize(("attributes", "dialect", "kind"), [
    ({"gen_ai.operation.name": "chat", "gen_ai.request.model": "m"}, "gen_ai", "llm"),
    ({"gen_ai.operation.name": "invoke_agent"}, "gen_ai", "agent"),
    ({"openinference.span.kind": "LLM"}, "openinference", "llm"),
    ({"openinference.span.kind": "TOOL"}, "openinference", "tool"),
    ({"openinference.span.kind": "CHAIN"}, "openinference", "chain"),
    ({"openinference.span.kind": "AGENT"}, "openinference", "chain"),
    ({"openinference.span.kind": "GUARDRAIL"}, "openinference", "other"),
    ({"traceloop.span.kind": "task"}, "openllmetry", "other"),
    ({"http.method": "GET"}, "unknown", "other"),
])
def test_dialect_detection_and_kind_mapping(attributes: dict[str, str], dialect: str,
                                            kind: str) -> None:
    span = normalize(raw_request([{"span_id": b"\x05" * 8, "attributes": attributes}]),
                     IngestPolicy()).spans[0]
    assert (span.dialect, span.kind) == (dialect, kind)


def test_attributes_are_bounded() -> None:
    attributes = {f"k{i:03d}": "v" * 3000 for i in range(60)}
    attributes["huge"] = "z" * 10_000
    span = normalize(raw_request([{"span_id": b"\x06" * 8, "attributes": attributes}]),
                     IngestPolicy(max_attribute_value_chars=1000,
                                  max_span_attributes_bytes=8192)).spans[0]
    encoded = json.dumps(span.attributes)
    assert len(encoded) < 8192 + 200
    assert span.attributes["rootsignal.server.attributes_truncated"] is True
    assert all(len(v) <= 1000 + len("…[truncated]") for v in span.attributes.values()
               if isinstance(v, str))


def test_pricing_marks_unknown_models_as_unpriced() -> None:
    def call(provider: str, model: str) -> None:
        with rs.agent_run("a"), rs.llm_call(model, provider=provider) as c:
            c.set_response(input_tokens=1_000_000, output_tokens=500_000)

    prices = PriceTable(models={("acme", "m1"): ModelPrice(Decimal("2"), Decimal("8"))})
    priced = by_kind(normalize(sdk_request(lambda: call("acme", "m1")),
                               IngestPolicy(prices=prices)).spans, "llm")[0]
    assert priced.cost_usd == Decimal("6.000000")
    unpriced = by_kind(normalize(sdk_request(lambda: call("acme", "other")),
                                 IngestPolicy(prices=prices)).spans, "llm")[0]
    assert unpriced.cost_usd is None


def test_args_hash_is_derived_for_spans_without_one() -> None:
    def tool_span(span_id: bytes, arguments: str) -> dict[str, object]:
        return {"span_id": span_id, "attributes": {
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup",
            "gen_ai.tool.call.arguments": arguments}}

    request = raw_request([
        tool_span(b"\x01" * 8, '{"sku": "A-1", "warehouse": "eu"}'),
        tool_span(b"\x02" * 8, '{"warehouse": "eu", "sku": "A-1"}'),  # same args, reordered
        tool_span(b"\x03" * 8, '{"sku": "B-2", "warehouse": "eu"}'),
    ])
    a, b, c = normalize(request, IngestPolicy(redact_on_ingest="never")).spans
    assert a.args_hash is not None and len(a.args_hash) == 16
    assert a.args_hash == b.args_hash != c.args_hash
    assert a.attributes["rootsignal.server.args_hash_derived"] is True


def test_sdk_args_hash_is_kept() -> None:
    def call() -> None:
        with rs.agent_run("a"), rs.tool_call("lookup", arguments={"x": 1}, side_effect="read"):
            pass

    span = next(s for s in normalize(sdk_request(call), IngestPolicy()).spans if s.kind == "tool")
    assert span.args_hash is not None
    assert "rootsignal.server.args_hash_derived" not in span.attributes
