from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from opentelemetry.trace import StatusCode

import rootsignal as rs
from conftest import Collect, by_name, one
from rootsignal import _attributes as A


@rs.tool(side_effect="read")
def get_account(account_id: str, include_history: bool = False) -> dict[str, Any]:
    """Fetch an account."""
    return {"id": account_id, "tier": "pro"}


@rs.tool("create_ticket", side_effect=rs.SideEffect.WRITE)
def make_ticket(summary: str) -> str:
    return "T-1"


class UpstreamTimeout(Exception):
    pass


@rs.tool(side_effect="read")
def flaky(account_id: str) -> None:
    raise UpstreamTimeout(f"timeout looking up {account_id} for jane@example.com")


def test_nested_sync_run_builds_one_trace_with_correct_parents(collect: Collect) -> None:
    @rs.agent_run("support-agent", agent_version="v1", prompt_version="p3", max_steps=5)
    def handle(request: str) -> str:
        with rs.llm_call("llama3.1:8b", provider="ollama",
                         input_messages=[{"role": "user", "content": request}]) as call:
            call.set_response([{"role": "assistant", "content": "ok"}],
                              input_tokens=12, output_tokens=3, finish_reasons=["stop"])
        get_account("acc-1")
        make_ticket("help")
        return "done"

    assert handle("hi") == "done"
    spans = collect()
    assert len(spans) == 4
    root = one(spans, "invoke_agent support-agent")
    llm = one(spans, "chat llama3.1:8b")
    lookup = one(spans, "execute_tool get_account")
    ticket = one(spans, "execute_tool create_ticket")

    assert root.parent is None
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    for child in (llm, lookup, ticket):
        assert child.parent is not None and child.parent.span_id == root.context.span_id

    assert root.attributes[A.RS_SPAN_KIND] == "agent"
    assert root.attributes[A.GEN_AI_AGENT_VERSION] == "v1"
    assert root.attributes[A.RS_PROMPT_VERSION] == "p3"
    assert root.attributes[A.RS_RUN_MAX_STEPS] == 5
    assert root.attributes[A.RS_RUN_OUTPUT] == "done"  # strings are stored verbatim

    assert llm.attributes[A.GEN_AI_USAGE_INPUT_TOKENS] == 12
    assert llm.attributes[A.GEN_AI_USAGE_OUTPUT_TOKENS] == 3
    assert llm.attributes[A.RS_LLM_CALL_INDEX] == 0
    assert tuple(llm.attributes[A.GEN_AI_RESPONSE_FINISH_REASONS]) == ("stop",)

    assert lookup.attributes[A.RS_TOOL_SIDE_EFFECT] == "read"
    assert lookup.attributes[A.GEN_AI_TOOL_DESCRIPTION] == "Fetch an account."
    assert json.loads(lookup.attributes[A.GEN_AI_TOOL_CALL_ARGUMENTS]) == {
        "account_id": "acc-1", "include_history": False}
    assert json.loads(lookup.attributes[A.GEN_AI_TOOL_CALL_RESULT]) == {
        "id": "acc-1", "tier": "pro"}
    assert ticket.attributes[A.RS_TOOL_SIDE_EFFECT] == "write"
    assert (lookup.attributes[A.RS_TOOL_CALL_INDEX], ticket.attributes[A.RS_TOOL_CALL_INDEX]) \
        == (0, 1)
    assert lookup.status.status_code is StatusCode.UNSET


async def test_concurrent_async_runs_keep_separate_trees(collect: Collect) -> None:
    @rs.tool(side_effect="none")
    async def compute(x: int) -> int:
        await asyncio.sleep(0.01)
        return x * 2

    @rs.agent_run("async-agent")
    async def run(n: int) -> int:
        a, b = await asyncio.gather(compute(n), compute(n + 1))
        return a + b

    results = await asyncio.gather(*(run(i) for i in range(5)))
    assert results == [4 * i + 2 for i in range(5)]

    spans = collect()
    roots = by_name(spans, "invoke_agent async-agent")
    assert len(roots) == 5
    root_ids = {r.context.span_id: r.context.trace_id for r in roots}
    tools = by_name(spans, "execute_tool compute")
    assert len(tools) == 10
    for t in tools:
        assert t.parent is not None
        assert root_ids[t.parent.span_id] == t.context.trace_id
    # Each run counts its own tool calls.
    per_trace: dict[int, list[int]] = {}
    for t in tools:
        per_trace.setdefault(t.context.trace_id, []).append(t.attributes[A.RS_TOOL_CALL_INDEX])
    assert all(sorted(v) == [0, 1] for v in per_trace.values())


def test_repeat_index_counts_identical_calls_only(collect: Collect) -> None:
    with rs.agent_run("loop"):
        for _ in range(3):
            get_account("ent-42")
        get_account("acc-7")
        get_account(account_id="ent-42", include_history=False)  # same call, kwargs form

    calls = by_name(collect(), "execute_tool get_account")
    calls.sort(key=lambda s: s.attributes[A.RS_TOOL_CALL_INDEX])
    assert [c.attributes[A.RS_TOOL_REPEAT_INDEX] for c in calls] == [0, 1, 2, 0, 3]
    hashes = {c.attributes[A.RS_TOOL_ARGS_HASH] for c in calls}
    assert len(hashes) == 2


def test_user_exception_propagates_unchanged_and_is_recorded_redacted(collect: Collect) -> None:
    with rs.agent_run("agent"), pytest.raises(UpstreamTimeout) as info:
        flaky("ent-42")
    assert "jane@example.com" in str(info.value)  # the caller's exception is untouched

    span = one(collect(), "execute_tool flaky")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes[A.ERROR_TYPE] == "UpstreamTimeout"
    event = next(e for e in span.events if e.name == "exception")
    assert "jane@example.com" not in event.attributes["exception.message"]
    assert "[REDACTED:EMAIL]" in event.attributes["exception.message"]
    assert "jane@example.com" not in event.attributes["exception.stacktrace"]
    assert "jane@example.com" not in (span.status.description or "")


def test_record_error_marks_soft_failures(collect: Collect) -> None:
    @rs.tool(side_effect="read")
    def search(q: str) -> dict[str, str]:
        scope = rs.current_scope()
        assert scope is not None
        scope.record_error("NotFound", "no documents")
        return {"error": "not found"}

    with rs.agent_run("agent"):
        search("x")
    span = one(collect(), "execute_tool search")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes[A.ERROR_TYPE] == "NotFound"


def test_user_input_is_not_captured_by_default(collect: Collect) -> None:
    messages = [{"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "my secret plan"}]
    with rs.agent_run("agent", input="my secret plan"), rs.llm_call("m", input_messages=messages):
        pass
    spans = collect()
    root = one(spans, "invoke_agent agent")
    assert A.RS_RUN_INPUT not in root.attributes
    assert root.attributes[A.RS_USER_INPUT_CAPTURED] is False
    sent = json.loads(one(spans, "chat m").attributes[A.GEN_AI_INPUT_MESSAGES])
    assert sent[0]["content"] == "You are helpful."
    assert sent[1] == {"role": "user", "content": "[not captured]"}


def test_unrecognised_message_shapes_are_dropped_when_user_input_is_off(collect: Collect) -> None:
    with rs.llm_call("m", input_messages="raw prompt with user text"):
        pass
    assert one(collect(), "chat m").attributes[A.GEN_AI_INPUT_MESSAGES] == "[not captured]"


def test_user_input_captured_when_enabled(configure: Any) -> None:
    collect = configure(capture_user_input=True)
    with rs.agent_run("agent", input={"q": "hello"}), \
            rs.llm_call("m", input_messages=[{"role": "user", "content": "hello"}]):
        pass
    spans = collect()
    assert json.loads(one(spans, "invoke_agent agent").attributes[A.RS_RUN_INPUT]) == {"q": "hello"}
    assert "hello" in one(spans, "chat m").attributes[A.GEN_AI_INPUT_MESSAGES]


def test_capture_payloads_off_drops_io_but_keeps_structure(configure: Any) -> None:
    collect = configure(capture_payloads=False)
    with rs.agent_run("agent"):
        get_account("acc-1")
    span = one(collect(), "execute_tool get_account")
    assert A.GEN_AI_TOOL_CALL_ARGUMENTS not in span.attributes
    assert A.GEN_AI_TOOL_CALL_RESULT not in span.attributes
    assert A.RS_TOOL_ARGS_HASH in span.attributes  # loops stay detectable


def test_payloads_are_redacted_and_truncated(configure: Any) -> None:
    collect = configure(max_payload_bytes=256)

    @rs.tool(side_effect="none")
    def echo(text: str) -> str:
        return text * 50

    with rs.agent_run("agent"):
        echo("contact bob@corp.io ")
    span = one(collect(), "execute_tool echo")
    args = span.attributes[A.GEN_AI_TOOL_CALL_ARGUMENTS]
    assert "bob@corp.io" not in args and "[REDACTED:EMAIL]" in args
    result = span.attributes[A.GEN_AI_TOOL_CALL_RESULT]
    assert len(result.encode()) <= 256 and result.endswith("…[truncated]")
    assert tuple(span.attributes[A.RS_TRUNCATED_FIELDS]) == (A.GEN_AI_TOOL_CALL_RESULT,)
    assert span.attributes[A.RS_REDACTED] is True


def test_redaction_can_be_disabled(configure: Any) -> None:
    collect = configure(redact_pii=False)
    with rs.agent_run("agent"):
        get_account("bob@corp.io")
    span = one(collect(), "execute_tool get_account")
    assert "bob@corp.io" in span.attributes[A.GEN_AI_TOOL_CALL_ARGUMENTS]


def test_retrieval_records_query_and_documents(collect: Collect) -> None:
    with rs.agent_run("rag"), rs.retrieval("refund policy", data_source="docs", top_k=3) as r:
        r.set_documents([{"id": "d1", "score": 0.82, "content": "Refunds within 30 days"}])
    span = one(collect(), "retrieval docs")
    assert span.attributes[A.RS_RETRIEVAL_RESULT_COUNT] == 1
    assert span.attributes[A.RS_RETRIEVAL_TOP_K] == 3
    assert span.attributes[A.GEN_AI_RETRIEVAL_QUERY_TEXT] == "refund policy"
    assert json.loads(span.attributes[A.GEN_AI_RETRIEVAL_DOCUMENTS])[0]["id"] == "d1"


def test_methods_exclude_self_from_arguments(collect: Collect) -> None:
    class Tools:
        @rs.tool(side_effect="none")
        def add(self, a: int, b: int) -> int:
            return a + b

    with rs.agent_run("agent"):
        assert Tools().add(1, 2) == 3
    span = one(collect(), "execute_tool add")
    assert json.loads(span.attributes[A.GEN_AI_TOOL_CALL_ARGUMENTS]) == {"a": 1, "b": 2}


def test_unserialisable_arguments_do_not_break_the_call(collect: Collect) -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("no repr for you")

    @rs.tool(side_effect="none")
    def take(obj: object) -> str:
        return "ok"

    with rs.agent_run("agent"):
        assert take(Hostile()) == "ok"
    assert one(collect(), "execute_tool take") is not None


def test_works_without_init() -> None:
    rs.shutdown()

    @rs.agent_run("agent")
    def run() -> int:
        return get_account("a")["tier"] == "pro"

    assert run() is True
    assert rs.flush() is True


def test_sample_rate_zero_exports_nothing_but_code_runs(configure: Any) -> None:
    collect = configure(sample_rate=0.0)
    with rs.agent_run("agent"):
        assert get_account("a")["id"] == "a"
    assert collect() == ()


def test_sample_rate_applies_per_trace(configure: Any) -> None:
    collect = configure(sample_rate=0.5, max_queue_size=8192, max_export_batch_size=512)
    for _ in range(2000):
        with rs.agent_run("agent"):
            get_account("a")
    spans = collect()
    roots = by_name(spans, "invoke_agent agent")
    assert 800 <= len(roots) <= 1200
    # Children follow their parent's decision: never an orphaned tool span.
    assert len(by_name(spans, "execute_tool get_account")) == len(roots)


def test_invalid_declarations_fail_at_decoration_time() -> None:
    with pytest.raises(ValueError, match="side_effect"):
        rs.tool(side_effect="sometimes")

    with pytest.raises(TypeError, match="generator"):
        @rs.tool
        def gen() -> Any:
            yield 1


def test_scopes_are_single_use(collect: Collect) -> None:
    scope = rs.agent_run("agent")
    with scope:
        pass
    with pytest.raises(RuntimeError, match="single-use"), scope:
        pass


def test_tool_call_context_manager(collect: Collect) -> None:
    with rs.agent_run("agent"), rs.tool_call("mcp_search", arguments={"q": "x"},
                                              side_effect="read", mcp_server="docs-mcp") as t:
        t.set_result(["a"])
    span = one(collect(), "execute_tool mcp_search")
    assert span.attributes[A.RS_MCP_SERVER] == "docs-mcp"
    assert json.loads(span.attributes[A.GEN_AI_TOOL_CALL_RESULT]) == ["a"]
