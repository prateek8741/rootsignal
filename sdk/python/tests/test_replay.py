from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import rootsignal as rs
from conftest import Collect, by_name, one
from rootsignal._serialize import args_hash
from rootsignal.replay import (
    RecordedToolError,
    ReplayDivergence,
    ReplayNetworkBlocked,
    ReplaySession,
    ReplayUnsupported,
    activate,
    network,
    stub,
)


class UpstreamTimeout(Exception):
    pass


CANARY: list[str] = []


@rs.tool(side_effect="read")
def get_account(account_id: str) -> dict[str, Any]:
    raise AssertionError("a recorded read tool must not run during recorded replay")


@rs.tool(side_effect="write")
def create_ticket(summary: str) -> dict[str, str]:
    CANARY.append(summary)  # a real side effect
    return {"ticket": "REAL"}


@rs.tool(side_effect="none")
def normalise(text: str) -> str:
    return text.strip().lower()


def fixture(index: int, tool: str, args: dict[str, Any], outcome: str = "result",
            result: Any = None, error_type: str | None = None) -> dict[str, Any]:
    return {"call_index": index, "tool": tool, "args_hash": args_hash(tool, args, None),
            "arguments": {"captured": True, "value": args}, "outcome": outcome,
            "result": {"captured": True, "value": result} if outcome == "result" else None,
            "error_type": error_type, "error_message": "timed out" if error_type else None}


def spec(tools: list[dict[str, Any]], llm: list[Any] | None = None) -> dict[str, Any]:
    return {"fixtures": {"tools": tools, "llm": [
        {"call_index": i, "output": {"captured": True, "value": v}}
        for i, v in enumerate(llm or [])]}}


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    CANARY.clear()
    yield
    CANARY.clear()


def test_recorded_errors_are_re_raised_as_the_original_class(collect: Collect) -> None:
    session = ReplaySession(spec([fixture(0, "get_account", {"account_id": "ent-1"}, "error",
                                          error_type="UpstreamTimeout")]))
    with activate(session), rs.agent_run("a"), pytest.raises(UpstreamTimeout, match="timed out"):
        get_account("ent-1")
    assert session.report.served_tools == 1
    span = one(collect(), "execute_tool get_account")
    assert span.attributes["error.type"] == "UpstreamTimeout"


def test_unknown_error_classes_become_recorded_tool_errors() -> None:
    session = ReplaySession(spec([fixture(0, "get_account", {"account_id": "x"}, "error",
                                          error_type="VanishedError")]))
    with activate(session), pytest.raises(RecordedToolError) as info:
        get_account("x")
    assert info.value.error_type == "VanishedError"


def test_results_are_served_and_identical_calls_reuse_the_last_outcome() -> None:
    session = ReplaySession(spec([fixture(0, "get_account", {"account_id": "a"},
                                          result={"tier": "pro"})]))
    with activate(session):
        assert get_account("a") == {"tier": "pro"}
        assert get_account(account_id="a") == {"tier": "pro"}  # same call, kwargs form
    assert (session.report.served_tools, session.report.reused_tools) == (2, 1)


def test_write_tools_never_execute(collect: Collect) -> None:
    recorded = ReplaySession(spec([fixture(0, "create_ticket", {"summary": "x"},
                                           result={"ticket": "T-9"})]))
    with activate(recorded):
        assert create_ticket("x") == {"ticket": "T-9"}  # recorded result, no execution
    unrecorded = ReplaySession(spec([]))
    with activate(unrecorded):
        assert create_ticket("new call") is None  # stubbed
    assert CANARY == []
    assert unrecorded.report.stubs == [{"tool": "create_ticket",
                                        "arguments": {"summary": "new call"}}]
    stub("create_ticket", lambda summary: {"ticket": "STUB"})
    with activate(ReplaySession(spec([]))):
        assert create_ticket("again") == {"ticket": "STUB"}
    assert CANARY == []
    spans = by_name(collect(), "execute_tool create_ticket")
    assert sum(1 for s in spans if s.attributes.get("rootsignal.replay.served")) == 1
    assert sum(1 for s in spans if s.attributes.get("rootsignal.replay.stubbed")) == 2


def test_unmatched_reads_fail_closed_and_cannot_be_swallowed() -> None:
    session = ReplaySession(spec([]))
    with activate(session), pytest.raises(ReplayDivergence):
        try:
            get_account("unrecorded")
        except Exception:  # agent code catching broadly must not hide a divergence
            pytest.fail("ReplayDivergence was swallowed by 'except Exception'")
    assert "no recorded result for 'get_account'" in session.report.divergences[0]


def test_pure_tools_run_live_and_reads_only_when_allowed() -> None:
    with activate(ReplaySession(spec([]))):
        assert normalise("  Hi ") == "hi"
    permissive = ReplaySession(spec([]), allow_live_reads=True)
    with activate(permissive), pytest.raises(AssertionError, match="must not run"):
        get_account("live")  # executed for real because live reads were allowed
    assert permissive.report.live_tools == 1


def test_strict_order_requires_the_recorded_sequence() -> None:
    tools = [fixture(0, "get_account", {"account_id": "a"}, result=1),
             fixture(1, "get_account", {"account_id": "b"}, result=2)]
    with activate(ReplaySession(spec(tools), match="strict_order")), \
            pytest.raises(ReplayDivergence):
        get_account("b")  # out of order
    with activate(ReplaySession(spec(tools), match="strict_order")):
        assert (get_account("a"), get_account("b")) == (1, 2)


def test_tool_call_blocks_with_side_effects_are_unsupported() -> None:
    session = ReplaySession(spec([]))
    with activate(session):
        with rs.tool_call("compute", arguments={}, side_effect="none"):
            pass  # pure: fine
        with pytest.raises(ReplayUnsupported), rs.tool_call("send_email", side_effect="write"):
            pytest.fail("the body of a write tool_call must not run in replay")
    assert "send_email" in session.report.unsupported[0]


def test_model_calls_are_served_in_recorded_mode_and_live_in_live_mode(
        collect: Collect) -> None:
    calls: list[str] = []

    def model() -> dict[str, str]:
        calls.append("live")
        return {"action": "reply"}

    recorded = ReplaySession(spec([], llm=[{"action": "call_tool"}]))
    with activate(recorded), rs.agent_run("a"):
        with rs.llm_call("m") as c:
            assert c.run(model) == {"action": "call_tool"}
        with pytest.raises(ReplayDivergence, match="more model calls"), rs.llm_call("m") as c:
            c.run(model)
    assert calls == [] and recorded.report.served_model_calls == 1
    assert recorded.report.trace_id is not None and len(recorded.report.trace_id) == 32

    with activate(ReplaySession(spec([]), mode="live_model")), rs.llm_call("m") as c:
        assert c.run(model) == {"action": "reply"}
    assert calls == ["live"]

    uncontrolled = ReplaySession(spec([]))
    with activate(uncontrolled), rs.llm_call("m") as c:
        c.set_response(input_tokens=1, output_tokens=1)  # model called some other way
    assert "outside llm_call.run()" in uncontrolled.report.unsupported[0]


def test_run_records_the_returned_value_outside_replay(collect: Collect) -> None:
    with rs.agent_run("a"), rs.llm_call("m") as c:
        c.run(lambda: {"action": "reply", "text": "hi"})
    span = one(collect(), "chat m")
    assert '"action": "reply"' in span.attributes["gen_ai.output.messages"]


def test_network_guard(tmp_path: Path) -> None:
    allowed, blocked = socket.socket(), socket.socket()
    for server in (allowed, blocked):
        server.bind(("127.0.0.1", 0))
        server.listen()
    threading.Thread(target=lambda: allowed.accept(), daemon=True).start()
    hits: list[str] = []
    blocked_port = blocked.getsockname()[1]
    network.install([("127.0.0.1", allowed.getsockname()[1])], on_block=hits.append)
    try:
        with socket.create_connection(allowed.getsockname(), timeout=2):
            pass
        with pytest.raises(ReplayNetworkBlocked):
            socket.create_connection(("127.0.0.1", blocked_port), timeout=2)
    finally:
        network.uninstall()
        allowed.close()
        blocked.close()
    assert hits == [f"127.0.0.1:{blocked_port}"]
