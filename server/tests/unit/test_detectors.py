from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

import rootsignal as rs
import run_detector_eval
from otlp_factory import sdk_request
from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.detectors import (
    DETECTOR_NAMES,
    excessive_retries,
    is_timeout,
    repeated_tool_call,
    run_detectors,
    step_limit_reached,
    unhandled_exception,
)
from rootsignal_server.detection.model import Finding, SpanView, TraceView
from rootsignal_server.ingest.normalize import IngestPolicy, normalize


def trace_of(build: Callable[[], Any]) -> TraceView:
    batch = normalize(sdk_request(build), IngestPolicy())
    return TraceView.build(batch.spans[0].trace_id,
                           [SpanView.from_normalized(s) for s in batch.spans])


def llm(model: str = "m") -> None:
    with rs.llm_call(model, provider="ollama") as c:
        c.set_response(input_tokens=10, output_tokens=2)


class Boom(Exception):
    pass


def failing(name: str, exc: BaseException, *, side_effect: str = "read",
            args: Any = None) -> None:
    with contextlib.suppress(type(exc)), rs.tool_call(name, arguments=args or {"x": 1},
                                                      side_effect=side_effect):
        raise exc


def ok_tool(name: str, *, side_effect: str = "read", args: Any = None) -> None:
    with rs.tool_call(name, arguments=args or {"x": 1}, side_effect=side_effect) as t:
        t.set_result("ok")


def detectors_fired(findings: list[Finding]) -> set[str]:
    return {f.detector for f in findings}


# --- individual detectors ------------------------------------------------------

def test_repeated_write_is_high_severity_with_evidence() -> None:
    def build() -> None:
        with rs.agent_run("a"):
            for _ in range(3):
                ok_tool("charge_card", side_effect="write", args={"amount": 10})

    trace = trace_of(build)
    [finding] = repeated_tool_call(trace, DetectorConfig())
    assert finding.severity == "high"
    assert finding.tool_name == "charge_card"
    assert finding.facts["calls"] == 3 and finding.facts["side_effect"] == "write"
    assert len(finding.evidence_span_ids) == 3
    assert set(finding.evidence_span_ids) <= set(trace.by_id)


def test_retries_count_only_back_to_back_attempts_after_failure() -> None:
    def alternating() -> None:
        with rs.agent_run("a"):
            for i in range(4):  # fail, ok, fail, ok: two single retries, never two in a row
                if i % 2 == 0:
                    failing("sync", Boom("x"), args={"id": 1})
                else:
                    ok_tool("sync", args={"id": 1})

    assert excessive_retries(trace_of(alternating), DetectorConfig()) == []

    def one_llm_error_then_normal_calls() -> None:
        with rs.agent_run("a"):
            with contextlib.suppress(Boom), rs.llm_call("m", provider="ollama"):
                raise Boom("transient")
            for _ in range(5):
                llm()

    # One retry (the next call succeeded); the later calls are normal steps, not retries.
    assert excessive_retries(trace_of(one_llm_error_then_normal_calls), DetectorConfig()) == []


def test_retry_storm_reports_recovery() -> None:
    def build() -> None:
        with rs.agent_run("a"):
            for _ in range(3):
                failing("sync", Boom("x"), args={"id": 1})
            ok_tool("sync", args={"id": 1})

    [finding] = excessive_retries(trace_of(build), DetectorConfig())
    assert finding.facts == {"retries": 3, "recovered": True, "threshold": 2,
                             "failed_attempts": 3}
    assert finding.severity == "low"  # it recovered


def test_nested_agent_exception_reported_once_with_origin() -> None:
    def build() -> None:
        with contextlib.suppress(Boom), rs.agent_run("outer"), rs.agent_run("inner"):
            failing_tool_raises()

    def failing_tool_raises() -> None:
        with rs.tool_call("explode", arguments={}, side_effect="none"):
            raise Boom("bad")

    trace = trace_of(build)
    [finding] = unhandled_exception(trace, DetectorConfig())
    outer = next(a for a in trace.agents if a.attributes["gen_ai.agent.name"] == "outer")
    explode = next(s for s in trace.spans if s.tool_name == "explode")
    assert finding.key == outer.span_id
    assert finding.evidence_span_ids == (outer.span_id, explode.span_id)
    assert finding.tool_name == "explode" and finding.error_type == "Boom"


def test_step_limit_counts_only_the_agents_own_model_calls() -> None:
    def build() -> None:
        with rs.agent_run("outer", max_steps=2):
            llm()
            with rs.agent_run("inner", max_steps=10):
                for _ in range(3):
                    llm()

    assert step_limit_reached(trace_of(build), DetectorConfig()) == []

    def at_limit() -> None:
        with rs.agent_run("outer", max_steps=2):
            llm()
            llm()

    [finding] = step_limit_reached(trace_of(at_limit), DetectorConfig())
    assert finding.facts == {"steps": 2, "max_steps": 2}


@pytest.mark.parametrize(("error_type", "message", "expected"), [
    ("TimeoutError", None, True),
    ("ReadTimeoutError", None, True),
    ("UpstreamTimeout", None, True),
    ("DeadlineExceeded", None, True),
    ("RuntimeError", "request timed out after 30s", True),
    ("ValueError", "invalid field 'timeout_seconds'", False),
    ("ValueError", None, False),
])
def test_timeout_classification(error_type: str, message: str | None, expected: bool) -> None:
    span = SpanView("a" * 16, None, "tool", "t", "error", message, error_type,
                    datetime(2026, 1, 1), None, None, None, None, None,
                    "t", "read", None, None)
    assert is_timeout(span) is expected


def test_healthy_trace_has_no_findings() -> None:
    def build() -> None:
        with rs.agent_run("a", max_steps=5):
            llm()
            ok_tool("lookup")
            llm()

    assert run_detectors(trace_of(build)) == []


# --- configuration ---------------------------------------------------------------

def test_config_overrides() -> None:
    def build() -> None:
        with rs.agent_run("a"):
            for _ in range(3):
                ok_tool("lookup")

    trace = trace_of(build)
    assert "repeated_tool_call" in detectors_fired(run_detectors(trace))
    raised = DetectorConfig.from_overrides({"repeated_tool_call": {"min_calls": 4}})
    assert "repeated_tool_call" not in detectors_fired(run_detectors(trace, raised))
    disabled = DetectorConfig.from_overrides({"repeated_tool_call": {"enabled": False}})
    assert run_detectors(trace, disabled) == []
    floats = DetectorConfig.from_overrides({"high_latency": {"max_trace_ms": 5}})
    assert floats.high_latency.max_trace_ms == 5.0


@pytest.mark.parametrize("overrides", [
    {"no_such_detector": {}},
    {"repeated_tool_call": {"min_cals": 4}},
    {"repeated_tool_call": {"min_calls": "4"}},
    {"repeated_tool_call": {"min_calls": True}},
    {"repeated_tool_call": {"min_calls": 0}},
    {"repeated_tool_call": {"enabled": "no"}},
    {"repeated_tool_call": 3},
])
def test_invalid_overrides_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DetectorConfig.from_overrides(overrides)


def test_every_detector_is_configurable() -> None:
    assert set(DETECTOR_NAMES) == set(DetectorConfig.__dataclass_fields__)


# --- dataset regression ---------------------------------------------------------

def test_detector_eval_matches_known_results() -> None:
    """Pins the detectors_v0 results; a change in either direction must be deliberate."""
    report = run_detector_eval.evaluate("detectors_v0")
    assert report.cases == 35
    mismatches = {(m["case"], tuple(m["false_positives"]), tuple(m["false_negatives"]))
                  for m in report.mismatches}
    assert mismatches == {
        ("polling_status", ("repeated_tool_call",), ()),
        ("cached_config_reads", ("repeated_tool_call",), ()),
    }


def test_dogfood_eval_matches_known_results() -> None:
    """Pins detection on the real dogfood traces (OpenInference + SDK, LangGraph)."""
    report = run_detector_eval.evaluate("dogfood_v0")
    assert report.cases == 51
    assert report.mismatches == []
    fired = {name: c.tp + c.fp for name, c in report.per_detector.items() if c.tp + c.fp}
    assert fired == {"tool_error": 2, "unhandled_exception": 1}
