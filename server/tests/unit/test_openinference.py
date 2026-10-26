"""OpenInference adapter and duplicate tool spans, on traces recorded from the
docs assistant (LangGraph 1.2 + openinference-instrumentation-langchain 0.1.79
+ the Rootsignal SDK, model llama3.1:8b on Ollama)."""

from __future__ import annotations

import json
from pathlib import Path

from otlp_factory import raw_request
from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.detectors import run_detectors
from rootsignal_server.detection.model import SpanView, TraceView
from rootsignal_server.ingest.normalize import IngestPolicy, NormalizedBatch, normalize
from rootsignal_server.ingest.otlp import JSON, decode_request

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "openinference"


def load(name: str) -> NormalizedBatch:
    request = decode_request((FIXTURES / f"{name}.json").read_bytes(), JSON)
    return normalize(request, IngestPolicy())


def view(batch: NormalizedBatch) -> TraceView:
    return TraceView.build(batch.spans[0].trace_id,
                           [SpanView.from_normalized(s) for s in batch.spans])


def test_openinference_spans_get_kinds_models_tokens_and_payloads() -> None:
    batch = load("langgraph_retrieve")
    by_dialect = {(s.dialect, s.kind) for s in batch.spans}
    assert {("rootsignal", "agent"), ("rootsignal", "tool"), ("openinference", "llm"),
            ("openinference", "tool"), ("openinference", "chain")} <= by_dialect
    llm = [s for s in batch.spans if s.kind == "llm"]
    assert llm and all(s.model == "llama3.1:8b" and s.provider == "ollama" for s in llm)
    assert all(s.input_tokens and s.output_tokens is not None for s in llm)
    # The flattened message attributes become the payload, as a list of messages.
    first = llm[0]
    assert not any(k.startswith(("llm.input_messages.", "llm.output_messages.", "input.value"))
                   for k in first.attributes)
    assert first.input_payload is not None and first.input_payload.content_type == "json"
    messages = json.loads(first.input_payload.body)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert first.output_payload is not None
    calls = json.loads(first.output_payload.body)[0]["tool_calls"]
    assert calls[0]["function.name"] == "retrieve"
    tool = next(s for s in batch.spans if s.dialect == "openinference" and s.kind == "tool")
    assert tool.tool_name == "retrieve" and tool.args_hash  # derived from input.value
    assert tool.input_payload is not None and tool.input_payload.body == "API key storage"


def test_sdk_redaction_claim_does_not_cover_third_party_spans() -> None:
    batch = load("langgraph_retrieve")
    ours = next(s for s in batch.spans if s.dialect == "rootsignal" and s.kind == "tool")
    theirs = next(s for s in batch.spans if s.dialect == "openinference" and s.kind == "llm")
    assert ours.input_payload is not None and ours.input_payload.redaction_state == "sdk"
    assert theirs.input_payload is not None and theirs.input_payload.redaction_state == "server"

    leaked = raw_request([{"span_id": b"\x01" * 8, "name": "ChatOllama", "attributes": {
        "openinference.span.kind": "LLM", "input.value": "mail me at jane@example.com"}}],
        resource={"service.name": "x", "rootsignal.sdk.redaction": "pattern"})
    payload = normalize(leaked, IngestPolicy()).spans[0].input_payload
    assert payload is not None and "jane@example.com" not in payload.body


def test_duplicate_tool_spans_count_once_for_detection() -> None:
    batch = load("langgraph_retrieve")
    tools = [s for s in batch.spans if s.kind == "tool"]
    assert len(tools) == 2  # OpenInference + SDK, for one call
    trace = view(batch)
    assert [(s.dialect, s.tool_name) for s in trace.spans if s.is_tool] == \
        [("rootsignal", "retrieve")]


def test_a_tool_error_is_one_failure_not_two() -> None:
    batch = load("langgraph_tool_error")
    failed_tools = [s for s in batch.spans if s.kind == "tool" and s.status == "error"]
    assert {s.dialect for s in failed_tools} == {"rootsignal", "openinference"}
    findings = run_detectors(view(batch), DetectorConfig())
    tool_errors = [f for f in findings if f.detector == "tool_error"]
    assert len(tool_errors) == 1 and tool_errors[0].tool_name == "read_file"
    # LangGraph's ToolNode re-raised the error, so it escaped the agent run.
    assert any(f.detector == "unhandled_exception" for f in findings)
