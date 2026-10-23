from __future__ import annotations

from typing import Any

import pytest

import rootsignal as rs
from otlp_factory import sdk_request
from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.model import SpanView, TraceView
from rootsignal_server.ingest.normalize import IngestPolicy, normalize
from rootsignal_server.regression.spec import Assertion
from rootsignal_server.replay.verdict import judge, next_case_status

CONFIG = DetectorConfig()


def run_with(llm_calls: int) -> TraceView:
    def build() -> None:
        with rs.agent_run("support"):
            for _ in range(llm_calls):
                with rs.llm_call("m", provider="ollama") as c:
                    c.set_response(input_tokens=10, output_tokens=2)

    batch = normalize(sdk_request(build), IngestPolicy())
    return TraceView.build(batch.spans[0].trace_id,
                           [SpanView.from_normalized(s) for s in batch.spans])


def assertion(id: str, role: str, *, requires: str = "recorded", max_calls: int = 2,
              enabled: bool = True) -> Assertion:
    return Assertion.model_validate({
        "id": id, "kind": "max_llm_calls", "params": {"max": max_calls}, "role": role,
        "requires": requires, "description": f"at most {max_calls} model calls",
        "enabled": enabled})


REPRODUCES = assertion("a1", "reproduces")
GUARD = assertion("g1", "guard", max_calls=10)
CLEAN: dict[str, Any] = {}


def verdict(assertions: list[Assertion], calls: int, *, mode: str = "recorded",
            report: dict[str, Any] = CLEAN) -> str:
    return judge(assertions, run_with(calls), mode=mode, report=report, config=CONFIG).verdict


def test_failure_present_and_absent() -> None:
    assert verdict([REPRODUCES, GUARD], 4) == "failing"
    outcome = judge([REPRODUCES, GUARD], run_with(1), mode="recorded", report=CLEAN,
                    config=CONFIG)
    assert outcome.verdict == "passing"
    assert [r["passed"] for r in outcome.results] == [True, True]


def test_a_failed_guard_means_broken_not_fixed() -> None:
    assert verdict([REPRODUCES, assertion("g1", "guard", max_calls=0)], 1) == "broken"


def test_divergence_overrides_everything() -> None:
    for report in ({"divergences": ["no recorded result for 'get_account'"]},
                   {"unsupported": ["tool_call() without @rs.tool"]},
                   {"blocked": ["example.com:443"]}):
        outcome = judge([REPRODUCES], run_with(4), mode="recorded", report=report, config=CONFIG)
        assert outcome.verdict == "diverged"
        assert outcome.summary.startswith("Replay diverged")


def test_assertions_outside_the_mode_are_skipped_and_can_make_it_inconclusive() -> None:
    live_tools = assertion("a2", "reproduces", requires="live_tools")
    outcome = judge([live_tools, GUARD], run_with(4), mode="recorded", report=CLEAN,
                    config=CONFIG)
    assert outcome.verdict == "inconclusive"
    assert outcome.results[0] == {**outcome.results[0], "evaluated": False}
    assert "needs live tools" in outcome.results[0]["reason"]

    live_model = assertion("a3", "reproduces", requires="live_model")
    assert verdict([live_model], 4, mode="recorded") == "inconclusive"
    assert verdict([live_model], 4, mode="live_model") == "failing"
    assert verdict([assertion("a4", "reproduces", enabled=False)], 4) == "inconclusive"


@pytest.mark.parametrize(("current", "purpose", "result", "expected"), [
    ("draft", "reproduce", "failing", "reproduced"),
    ("draft", "reproduce", "passing", "not_reproduced"),
    ("draft", "reproduce", "diverged", "draft"),
    ("reproduced", "verify", "passing", "passing"),
    ("passing", "verify", "failing", "failing"),
    ("failing", "verify", "passing", "passing"),
    ("draft", "verify", "passing", "draft"),  # never reproduced: a pass proves nothing
    ("not_reproduced", "verify", "passing", "not_reproduced"),
    ("draft", "verify", "failing", "draft"),
    ("reproduced", "verify", "inconclusive", "reproduced"),
    ("reproduced", "verify", "broken", "reproduced"),
])
def test_case_status_transitions(current: str, purpose: str, result: str,
                                 expected: str) -> None:
    status, note = next_case_status(current, purpose, result)
    assert status == expected
    if (purpose, result) == ("verify", "passing") and expected != "passing":
        assert note and "never reproduced" in note
