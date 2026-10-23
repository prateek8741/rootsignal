from __future__ import annotations

import contextlib
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import rootsignal as rs
from otlp_factory import sdk_request
from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.detectors import DETECTOR_NAMES, run_detectors
from rootsignal_server.detection.model import SpanView, TraceView
from rootsignal_server.ingest.normalize import IngestPolicy, normalize
from rootsignal_server.ingest.otlp import JSON, decode_request
from rootsignal_server.regression.assertions import GUARDS, evaluate, from_finding, select
from rootsignal_server.regression.spec import Assertion, CaseSpec

DATASET = Path(__file__).resolve().parents[3] / "evaluation" / "datasets" / "detectors_v0"
CONFIG = DetectorConfig()


def view_of(request: Any) -> TraceView:
    batch = normalize(request, IngestPolicy())
    return TraceView.build(batch.spans[0].trace_id,
                           [SpanView.from_normalized(s) for s in batch.spans])


def sdk_view(build: Callable[[], Any]) -> TraceView:
    return view_of(sdk_request(build))


def case_assertions(view: TraceView) -> tuple[list[Assertion], list[dict[str, str]]]:
    candidates = []
    for f in run_detectors(view, CONFIG):
        side_effect = next((s.side_effect for s in view.spans if s.tool_name == f.tool_name), None)
        candidates += from_finding(f.detector, "f", f.key, f.tool_name, f.facts, side_effect,
                                   CONFIG)
    return select([*candidates, *GUARDS], view, CONFIG)


def test_every_detector_yields_a_reproducing_assertion_on_the_dataset() -> None:
    labels = json.loads((DATASET / "labels.json").read_text())
    reproduced: dict[str, int] = defaultdict(int)
    for case_id, label in labels.items():
        if label["category"] != "positive":
            continue
        view = view_of(decode_request((DATASET / "cases" / f"{case_id}.json").read_bytes(), JSON))
        kept, _ = case_assertions(view)
        for assertion in kept:
            result = evaluate(assertion, view, CONFIG)
            if assertion.role == "reproduces":
                assert not result.passed, (case_id, assertion.description)  # captures the bug
                reproduced[assertion.derived_from["detector"]] += 1
            else:
                assert result.passed, (case_id, assertion.description)  # guards hold on source
    assert set(reproduced) == set(DETECTOR_NAMES), set(DETECTOR_NAMES) - set(reproduced)


class Timeout(Exception):
    pass


def retry_loop(calls: int) -> Callable[[], None]:
    def build() -> None:
        with rs.agent_run("support", max_steps=4):
            for _ in range(calls):
                with rs.llm_call("m", provider="ollama") as c:
                    c.set_response(input_tokens=10, output_tokens=2)
                with contextlib.suppress(Timeout), rs.tool_call(
                        "get_account", arguments={"id": "ent-1"}, side_effect="read"):
                    raise Timeout("timed out")
    return build


def test_assertions_fail_on_the_bug_and_pass_on_the_fix() -> None:
    kept, _ = case_assertions(sdk_view(retry_loop(4)))
    by_kind = {a.kind: a for a in kept}
    identical = by_kind["max_identical_tool_calls"]
    assert identical.params == {"tool": "get_account", "max": 2}
    assert identical.requires == "recorded" and identical.role == "reproduces"
    assert by_kind["max_llm_calls"].params == {"max": 3}

    fixed = sdk_view(retry_loop(1))  # the fix: one attempt, then give up honestly
    results = {a.kind: evaluate(a, fixed, CONFIG).passed for a in kept}
    assert results["max_identical_tool_calls"] and results["max_llm_calls"]
    assert results["run_completed"]
    # The tool still times out in the fixed run: that part can only pass with live tools.
    timeout = next(a for a in kept if a.derived_from.get("detector") == "timeout")
    assert timeout.requires == "live_tools" and not evaluate(timeout, fixed, CONFIG).passed


def test_requirement_per_failure_kind() -> None:
    def repeated_write() -> None:
        with rs.agent_run("a"):
            for _ in range(3):
                with rs.tool_call("charge_card", arguments={"amount": 5}, side_effect="write"):
                    pass

    kept, _ = case_assertions(sdk_view(repeated_write))
    write = next(a for a in kept if a.kind == "max_identical_tool_calls")
    assert write.params["max"] == 1  # a write must never be duplicated

    def tokens() -> None:
        with rs.agent_run("a"), rs.llm_call("m", provider="ollama") as c:
            c.set_response(input_tokens=90_000, output_tokens=20_000)

    kept, _ = case_assertions(sdk_view(tokens))
    assert {a.requires for a in kept if a.role == "reproduces"} == {"live_model"}


def test_unhandled_exception_case_and_guard_deduplication() -> None:
    def crash() -> None:
        with contextlib.suppress(ValueError), rs.agent_run("a"):
            raise ValueError("bad input")

    kept, dropped = case_assertions(sdk_view(crash))
    kinds = [(a.kind, a.role) for a in kept]
    assert ("no_unhandled_exception", "reproduces") in kinds
    assert ("no_unhandled_exception", "guard") not in kinds  # same check, not duplicated
    assert [a.id for a in kept] == [f"a{i + 1}" for i in range(len(kept))]
    assert all("reason" in d for d in dropped)


def test_spec_round_trip_and_strictness() -> None:
    document = {
        "schema_version": 1, "name": "n", "entrypoint": "pkg.mod:run",
        "source": {"trace_id": "a" * 32, "issue_id": "i", "agent_name": None,
                   "agent_version": None, "started_at": "2026-10-23T10:00:00+00:00",
                   "completion_reason": "root_span"},
        "input": {"kind": "kwargs", "value": {"q": "hi"}},
        "fixtures": {"tools": [{"call_index": 0, "tool": "t", "side_effect": "read",
                                "args_hash": "h", "repeat_index": 0,
                                "arguments": {"captured": True, "value": {"x": 1}},
                                "outcome": "error", "error_type": "E", "error_message": "m"}]},
        "assertions": [{"id": "a1", "kind": "run_completed", "description": "d",
                        "role": "guard", "requires": "recorded"}],
        "quality": {"replayable_recorded": True},
    }
    spec = CaseSpec.model_validate(document)
    assert CaseSpec.model_validate_json(spec.model_dump_json()) == spec
    for bad in ({**document, "schema_version": 2}, {**document, "surprise": 1},
                {**document, "assertions": [{**document["assertions"][0], "kind": "vibes"}]}):
        with pytest.raises(ValidationError):
            CaseSpec.model_validate(bad)
