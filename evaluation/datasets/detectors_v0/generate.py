"""Generate the detectors_v0 labelled dataset.

Each case is one agent run recorded with the real SDK and written as OTLP/JSON
(the wire format, hex IDs), plus a label: the set of detectors a reviewer would
want to fire on that trace. See README.md in this directory for the rubric.

Timing for latency cases is set by editing span end times after recording;
everything else is exactly what the SDK emitted.

    uv run python evaluation/datasets/detectors_v0/generate.py
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.protobuf import json_format
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import rootsignal as rs

HERE = Path(__file__).resolve().parent
Doc = dict[str, Any]


@dataclass(frozen=True)
class Case:
    id: str
    expected: frozenset[str]
    category: str  # positive | negative | hard
    description: str
    build: Callable[[], Any]
    post: Callable[[Doc], None] | None = None
    note: str = ""
    raw: Doc | None = field(default=None)  # hand-built OTLP for non-SDK dialects


# --- tools -------------------------------------------------------------------

class UpstreamTimeout(Exception):
    pass


class RateLimitError(Exception):
    pass


@rs.tool(side_effect="read")
def get_account(account_id: str) -> dict[str, str]:
    if account_id.startswith("ent-"):
        raise UpstreamTimeout(f"account service timed out for {account_id}")
    return {"id": account_id, "tier": "pro"}


@rs.tool(side_effect="none")
def search_docs(query: str) -> list[dict[str, Any]]:
    return [] if "unknown" in query else [{"id": "d1", "content": "Refunds take 5 days."}]


@rs.tool(side_effect="read")
def list_orders(customer_id: str, page: int) -> list[str]:
    return [f"ORD-{page}{i}" for i in range(3)]


@rs.tool(side_effect="write")
def create_ticket(summary: str) -> str:
    return "T-100"


@rs.tool(side_effect="read")
def check_job_status(job_id: str) -> str:
    return "running"


@rs.tool(side_effect="read")
def get_config(key: str) -> str:
    return "v"


def call_tool(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def llm(tokens: tuple[int, int] = (200, 40), model: str = "llama3.1:8b") -> None:
    with rs.llm_call(model, provider="ollama",
                     input_messages=[{"role": "user", "content": "help"}]) as c:
        c.set_response([{"role": "assistant", "content": "ok"}],
                       input_tokens=tokens[0], output_tokens=tokens[1])


def failing_tool(name: str, exc: BaseException, side_effect: str = "read",
                 arguments: Any = None) -> None:
    with contextlib.suppress(type(exc)), rs.tool_call(
            name, arguments=arguments or {"id": 1}, side_effect=side_effect):
        raise exc


# --- scenarios -----------------------------------------------------------------

def unhandled_from_tool() -> None:
    @rs.tool(side_effect="read")
    def parse_invoice(doc_id: str) -> None:
        raise ValueError("invoice has no total")

    with contextlib.suppress(ValueError), rs.agent_run("billing-agent"):
        llm()
        parse_invoice("inv-1")


def unhandled_in_agent_body() -> None:
    with contextlib.suppress(KeyError), rs.agent_run("billing-agent"):
        llm()
        raise KeyError("customer_id")


def nested_agents_exception() -> None:
    with contextlib.suppress(RuntimeError), rs.agent_run("planner"):
        llm()
        with rs.agent_run("executor"):
            llm()
            raise RuntimeError("plan step 3 invalid")


def tool_error_handled() -> None:
    with rs.agent_run("support-agent"):
        llm()
        failing_tool("delete_file", PermissionError("read-only filesystem"), "write")
        llm()


def tool_soft_error() -> None:
    with rs.agent_run("support-agent"):
        llm()
        with rs.tool_call("lookup_customer", arguments={"email_hash": "ab12"},
                          side_effect="read") as t:
            t.record_error("NotFound", "no such customer")
            t.set_result({"error": "not found"})
        llm()


def timeout_tool() -> None:
    with rs.agent_run("support-agent"):
        llm()
        failing_tool("fetch_url", TimeoutError("read timed out"))
        llm()


def timeout_llm() -> None:
    with rs.agent_run("support-agent"):
        with contextlib.suppress(UpstreamTimeout), rs.llm_call("llama3.1:8b", provider="ollama"):
            raise UpstreamTimeout("model server did not answer")
        llm()


def timeout_by_message() -> None:
    with rs.agent_run("support-agent"):
        llm()
        failing_tool("query_db", RuntimeError("request timed out after 30s"))


def repeated_empty_search() -> None:
    with rs.agent_run("docs-agent"):
        for _ in range(4):
            llm()
            search_docs("unknown error code E42")


def retry_loop_until_step_limit() -> None:
    with rs.agent_run("support-agent", max_steps=5) as run:
        for _ in range(5):
            llm()
            call_tool(get_account, "ent-42")
        run.set_output("I've updated your account.")


def retry_then_success() -> None:
    attempts = iter([True, True, False])

    @rs.tool("sync_crm", side_effect="read")
    def sync_crm(record: str) -> str:
        if next(attempts):
            raise ConnectionError("connection reset")
        return "ok"

    with rs.agent_run("crm-agent"):
        llm()
        for _ in range(3):
            if call_tool(sync_crm, "r-9") == "ok":
                break


def repeated_write() -> None:
    with rs.agent_run("support-agent"):
        for _ in range(3):
            llm()
            create_ticket("printer broken")


def step_limit_no_tools() -> None:
    with rs.agent_run("writer-agent", max_steps=3):
        for _ in range(3):
            llm()


def token_budget_trace() -> None:
    with rs.agent_run("research-agent"):
        for _ in range(3):
            llm((28_000, 22_000))


def token_budget_call() -> None:
    with rs.agent_run("research-agent"):
        llm((38_000, 4_000))


def plain_run() -> None:
    with rs.agent_run("support-agent"):
        llm()
        get_account("acc-1")
        llm()


def llm_retries() -> None:
    with rs.agent_run("support-agent"):
        for _ in range(3):
            with contextlib.suppress(RateLimitError), rs.llm_call("llama3.1:8b",
                                                                  provider="ollama"):
                raise RateLimitError("429 from model server")
        llm()


def happy_rag() -> None:
    with rs.agent_run("docs-agent"):
        with rs.retrieval("refund policy", data_source="docs", top_k=3) as r:
            r.set_documents([{"id": "d1", "score": 0.8, "content": "30 days"}])
        llm()
        search_docs("refund policy")
        llm()


def pagination() -> None:
    with rs.agent_run("orders-agent"):
        llm()
        for page in range(1, 5):
            list_orders("cus_1", page)
        llm()


def single_retry_success() -> None:
    attempts = iter([True, False])

    @rs.tool("sync_crm", side_effect="read")
    def sync_crm(record: str) -> str:
        if next(attempts):
            raise ConnectionError("connection reset")
        return "ok"

    with rs.agent_run("crm-agent"):
        llm()
        for _ in range(2):
            if call_tool(sync_crm, "r-9") == "ok":
                break


def twice_identical() -> None:
    with rs.agent_run("docs-agent"):
        llm()
        search_docs("refund policy")
        search_docs("refund policy")


def under_step_limit() -> None:
    with rs.agent_run("writer-agent", max_steps=5):
        for _ in range(4):
            llm()


def moderate_tokens() -> None:
    with rs.agent_run("research-agent"):
        llm((15_000, 5_000))


def caught_in_agent_body() -> None:
    with rs.agent_run("support-agent"):
        llm()
        with contextlib.suppress(ZeroDivisionError):
            _ = 1 / 0
        llm()


def two_tools_twice_each() -> None:
    with rs.agent_run("support-agent"):
        for _ in range(2):
            llm()
            get_account("acc-1")
            search_docs("refund policy")


def polling_status() -> None:
    with rs.agent_run("batch-agent"):
        llm()
        for _ in range(5):
            check_job_status("job-7")
        llm()


def cached_config_reads() -> None:
    with rs.agent_run("support-agent"):
        for _ in range(3):
            get_config("locale")
        llm()


def timeout_word_not_timeout() -> None:
    with rs.agent_run("settings-agent"):
        llm()
        failing_tool("update_settings", ValueError("invalid field 'timeout_seconds'"), "write")


def false_completion_after_error() -> None:
    with rs.agent_run("support-agent") as run:
        llm()
        failing_tool("refund_payment", PermissionError("refunds disabled for account"), "write")
        llm()
        run.set_output("Your refund has been issued.")


def timeout_then_fallback() -> None:
    with rs.agent_run("support-agent"):
        llm()
        failing_tool("primary_search", TimeoutError("search timed out"), "none")
        search_docs("refund policy")
        llm()


def rootless_tool_error() -> None:
    with rs.agent_run("support-agent"):
        llm()
        failing_tool("export_csv", OSError("disk full"), "write")


# --- timing edits ----------------------------------------------------------------

def _spans(doc: Doc) -> list[Doc]:
    return [s for rs_ in doc["resourceSpans"] for ss in rs_["scopeSpans"] for s in ss["spans"]]


def set_duration(name_prefix: str, ms: float) -> Callable[[Doc], None]:
    """Stretch matching spans to ``ms`` and push later spans and ancestors along."""
    def edit(doc: Doc) -> None:
        spans = _spans(doc)
        by_id = {s["spanId"]: s for s in spans}
        for span in spans:
            if not span["name"].startswith(name_prefix):
                continue
            start, old_end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
            new_end = start + int(ms * 1e6)
            shift = new_end - old_end
            span["endTimeUnixNano"] = str(new_end)
            for other in spans:
                if other is not span and int(other["startTimeUnixNano"]) >= old_end:
                    other["startTimeUnixNano"] = str(int(other["startTimeUnixNano"]) + shift)
                    other["endTimeUnixNano"] = str(int(other["endTimeUnixNano"]) + shift)
            parent = by_id.get(span.get("parentSpanId", ""))
            while parent is not None:
                parent["endTimeUnixNano"] = str(int(parent["endTimeUnixNano"]) + shift)
                parent = by_id.get(parent.get("parentSpanId", ""))
    return edit


def drop_root(doc: Doc) -> None:
    for rs_ in doc["resourceSpans"]:
        for ss in rs_["scopeSpans"]:
            ss["spans"] = [s for s in ss["spans"] if s.get("parentSpanId")]


# --- non-SDK dialect ----------------------------------------------------------

def gen_ai_dialect_loop() -> Doc:
    """Spans as another OTel GenAI instrumentation might send them: no rootsignal.*
    attributes, so no args hash; arguments travel in gen_ai.tool.call.arguments."""
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    base = 1_760_000_000_000_000_000
    spans = [{"traceId": trace_id, "spanId": "00f067aa0ba902b7", "name": "invoke_agent shop",
              "startTimeUnixNano": str(base), "endTimeUnixNano": str(base + 9_000_000_000),
              "attributes": [{"key": "gen_ai.operation.name",
                              "value": {"stringValue": "invoke_agent"}}]}]
    for i in range(3):
        start = base + (i + 1) * 2_000_000_000
        spans.append({
            "traceId": trace_id, "spanId": f"00f067aa0ba902c{i}",
            "parentSpanId": "00f067aa0ba902b7", "name": "execute_tool inventory_lookup",
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 300_000_000),
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                {"key": "gen_ai.tool.name", "value": {"stringValue": "inventory_lookup"}},
                {"key": "gen_ai.tool.call.arguments",
                 "value": {"stringValue": '{"sku": "A-1", "warehouse": "eu"}'}}]})
    return {"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "shop-agent"}}]},
        "scopeSpans": [{"spans": spans}]}]}


# --- registry --------------------------------------------------------------------

def E(*names: str) -> frozenset[str]:
    return frozenset(names)


CASES: tuple[Case, ...] = (
    Case("unhandled_from_tool", E("unhandled_exception", "tool_error"), "positive",
         "Tool raises ValueError; the agent doesn't catch it.", unhandled_from_tool),
    Case("unhandled_in_agent_body", E("unhandled_exception"), "positive",
         "Agent code raises KeyError outside any tool.", unhandled_in_agent_body),
    Case("nested_agents_exception", E("unhandled_exception"), "positive",
         "Sub-agent raises; the error escapes both agents.", nested_agents_exception),
    Case("tool_error_handled", E("tool_error"), "positive",
         "Write tool fails with PermissionError; the agent carries on.", tool_error_handled),
    Case("tool_soft_error", E("tool_error"), "positive",
         "Tool returns an error object and marks the span via record_error.", tool_soft_error),
    Case("timeout_tool", E("timeout"), "positive", "Tool raises TimeoutError.", timeout_tool),
    Case("timeout_llm", E("timeout"), "positive", "Model call times out once.", timeout_llm),
    Case("timeout_by_message", E("timeout"), "positive",
         "RuntimeError whose message says the request timed out.", timeout_by_message),
    Case("repeated_empty_search", E("repeated_tool_call"), "positive",
         "Agent searches the same query 4 times; nothing fails, it just loops.",
         repeated_empty_search),
    Case("retry_loop_until_step_limit",
         E("timeout", "repeated_tool_call", "excessive_retries", "step_limit_reached"),
         "positive", "The slice's seeded bug: identical failing lookup until max_steps, "
         "then claims success.", retry_loop_until_step_limit,
         note="The false success claim is semantic; no deterministic detector covers it."),
    Case("retry_then_success", E("tool_error", "repeated_tool_call", "excessive_retries"),
         "positive", "Two identical failed attempts, third succeeds.", retry_then_success),
    Case("repeated_write", E("repeated_tool_call"), "positive",
         "create_ticket (write) called 3 times with identical arguments.", repeated_write),
    Case("step_limit_no_tools", E("step_limit_reached"), "positive",
         "Agent spends all 3 steps on model calls.", step_limit_no_tools),
    Case("token_budget_trace", E("token_budget_exceeded"), "positive",
         "Three calls totalling 150k tokens.", token_budget_trace),
    Case("token_budget_call", E("token_budget_exceeded"), "positive",
         "One 42k-token call.", token_budget_call),
    Case("latency_trace", E("high_latency"), "positive",
         "Whole run takes 90 s (spread over two model calls).", plain_run,
         post=set_duration("chat", 45_000)),
    Case("latency_tool", E("high_latency"), "positive",
         "One tool call takes 45 s.", plain_run, post=set_duration("execute_tool", 45_000)),
    Case("llm_retries", E("excessive_retries"), "positive",
         "Model call rate-limited three times in a row, then succeeds.", llm_retries),
    Case("gen_ai_dialect_loop", E("repeated_tool_call"), "positive",
         "Non-Rootsignal GenAI spans: same lookup 3 times; no SDK args hash.",
         lambda: None, raw=gen_ai_dialect_loop()),
    Case("rootless_tool_error", E("tool_error"), "positive",
         "Root span lost (e.g. process killed); a write tool failed.", rootless_tool_error,
         post=drop_root),

    Case("happy_path", E(), "negative", "Model, read tool, model.", plain_run),
    Case("happy_rag", E(), "negative", "Retrieval, search, two model calls.", happy_rag),
    Case("pagination", E(), "negative", "Same tool, 4 different pages.", pagination),
    Case("twice_identical", E(), "negative", "Identical search twice (below threshold).",
         twice_identical),
    Case("under_step_limit", E(), "negative", "4 of 5 allowed steps.", under_step_limit),
    Case("moderate_tokens", E(), "negative", "20k tokens in one call.", moderate_tokens),
    Case("caught_in_agent_body", E(), "negative",
         "Exception raised and caught inside agent code, no span fails.", caught_in_agent_body),
    Case("two_tools_twice_each", E(), "negative",
         "Two different tools, each called twice.", two_tools_twice_each),
    Case("latency_under_threshold", E(), "negative", "Run takes 59 s.", plain_run,
         post=set_duration("chat", 29_000)),
    Case("single_retry_success", E("tool_error"), "negative",
         "One failure, one successful retry: tool_error yes, excessive_retries no.",
         single_retry_success),

    Case("polling_status", E(), "hard",
         "Agent polls a job status 5 times by design; nothing is wrong.", polling_status,
         note="Expected false positive for repeated_tool_call."),
    Case("cached_config_reads", E(), "hard",
         "Cheap config read repeated 3 times; harmless.", cached_config_reads,
         note="Expected false positive for repeated_tool_call."),
    Case("timeout_word_not_timeout", E("tool_error"), "hard",
         "Validation error mentioning 'timeout_seconds' is not a timeout.",
         timeout_word_not_timeout),
    Case("false_completion_after_error", E("tool_error"), "hard",
         "Refund fails but the agent tells the user it succeeded.",
         false_completion_after_error,
         note="Only the tool error is deterministically detectable; the false claim needs "
              "a semantic signal (Phase 2)."),
    Case("timeout_then_fallback", E("timeout"), "hard",
         "Primary search times out, fallback succeeds; still worth knowing.",
         timeout_then_fallback),
)


def record(case: Case) -> Doc:
    if case.raw is not None:
        copied: Doc = json.loads(json.dumps(case.raw))
        return copied
    exporter = InMemorySpanExporter()
    rs.init(rs.Config(service_name="detectors-v0"), exporter=exporter)
    try:
        case.build()
        rs.flush(5)
        spans = exporter.get_finished_spans()
    finally:
        rs.shutdown()
    doc = json_format.MessageToDict(encode_spans(spans))
    for span in _spans(doc):  # OTLP/JSON uses hex IDs; protobuf's JSON mapping uses base64
        for key in ("traceId", "spanId", "parentSpanId"):
            if span.get(key):
                span[key] = base64.b64decode(span[key]).hex()
    if case.post is not None:
        case.post(doc)
    return doc


def main() -> None:
    cases_dir = HERE / "cases"
    for old in cases_dir.glob("*.json"):
        old.unlink()
    labels = {}
    for case in CASES:
        (cases_dir / f"{case.id}.json").write_text(json.dumps(record(case), indent=1) + "\n")
        labels[case.id] = {"expected": sorted(case.expected), "category": case.category,
                           "description": case.description, "note": case.note}
    (HERE / "labels.json").write_text(json.dumps(labels, indent=2) + "\n")
    print(f"wrote {len(CASES)} cases to {cases_dir}")


if __name__ == "__main__":
    main()
