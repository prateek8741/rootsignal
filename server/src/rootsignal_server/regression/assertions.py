"""Assertions: generated from the detector that fired, evaluated on any trace.

Evaluation is pure (a ``TraceView`` in, pass/fail out) and reuses the
detectors, so "the failure is gone" means exactly "the detector that found
it no longer fires", with the project's detector configuration.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.detectors import DETECTORS, unhandled_exception
from rootsignal_server.detection.model import Finding, TraceView
from rootsignal_server.regression.spec import Assertion, AssertionKind, Requirement

_DETECTOR_FNS = {d.name: d.fn for d in DETECTORS}


@dataclass(frozen=True)
class AssertionResult:
    assertion_id: str
    passed: bool
    observed: str


def _detector_hits(trace: TraceView, detector: str, config: DetectorConfig,
                   tool: str | None) -> list[Finding]:
    findings = _DETECTOR_FNS[detector](trace, config)
    if tool is not None:
        findings = [f for f in findings if f.tool_name == tool]
    return findings


def evaluate(assertion: Assertion, trace: TraceView,
             config: DetectorConfig | None = None) -> AssertionResult:
    config = config or DetectorConfig()
    p = assertion.params
    kind = assertion.kind
    if kind == "detector_absent":
        hits = _detector_hits(trace, p["detector"], config, p.get("tool"))
        return AssertionResult(assertion.id, not hits,
                               f"{len(hits)} {p['detector']} finding(s)"
                               + (f": {hits[0].title}" if hits else ""))
    if kind == "max_identical_tool_calls":
        counts = Counter(s.args_hash for s in trace.spans
                         if s.is_tool and s.target == p["tool"] and s.args_hash)
        worst = max(counts.values(), default=0)
        return AssertionResult(assertion.id, worst <= p["max"],
                               f"at most {worst} identical call(s) to '{p['tool']}'")
    if kind == "max_llm_calls":
        calls = sum(1 for s in trace.spans if s.kind == "llm")
        return AssertionResult(assertion.id, calls <= p["max"], f"{calls} model call(s)")
    if kind == "no_unhandled_exception":
        hits = unhandled_exception(trace, config)
        return AssertionResult(assertion.id, not hits,
                               hits[0].title if hits else "no exception escaped the run")
    if kind == "run_completed":
        roots = [r for r in trace.roots if r.parent_span_id is None]
        done = bool(roots) and all(r.ended_at is not None for r in roots)
        return AssertionResult(assertion.id, done,
                               "the run's root span ended" if done else "no finished root span")
    if kind == "max_total_tokens":
        tokens = sum((s.input_tokens or 0) + (s.output_tokens or 0) for s in trace.spans
                     if s.kind == "llm")
        return AssertionResult(assertion.id, tokens <= p["max"], f"{tokens:,} tokens")
    if kind == "max_duration_ms":
        duration = trace.duration_ms() or 0.0
        return AssertionResult(assertion.id, duration <= p["max"], f"{duration:,.0f} ms")
    raise ValueError(f"unknown assertion kind {kind!r}")


# --- generation -----------------------------------------------------------------

def _tool_or_model(finding_key: str, tool: str | None) -> tuple[str | None, bool]:
    """(tool name, is about a model) for keys like 'tool:x:hash' / 'llm:model' / 'tool:name'."""
    if tool:
        return tool, False
    return None, finding_key.startswith("llm:")


def from_finding(detector: str, finding_id: str, finding_key: str, tool: str | None,
                 facts: Mapping[str, Any], side_effect: str | None,
                 config: DetectorConfig) -> list[Assertion]:
    """Assertions that encode one finding (role 'reproduces')."""
    origin = {"detector": detector, "finding_id": finding_id}

    def make(kind: AssertionKind, params: dict[str, Any], description: str,
             requires: Requirement) -> Assertion:
        return Assertion(id="", kind=kind, params=params, description=description,
                         role="reproduces", requires=requires, derived_from=origin)

    target, about_model = _tool_or_model(finding_key, tool)
    if detector == "repeated_tool_call" and target:
        threshold = int(facts.get("threshold") or config.repeated_tool_call.min_calls)
        # A write must never be duplicated; for reads, stay below the detector threshold.
        limit = 1 if side_effect == "write" else threshold - 1
        return [make("max_identical_tool_calls", {"tool": target, "max": limit},
                     f"'{target}' is called at most {limit} time(s) with identical arguments",
                     "recorded")]
    if detector == "excessive_retries":
        scope = {"tool": target} if target else {}
        return [make("detector_absent", {"detector": detector, **scope},
                     f"No back-to-back retry storm of '{target or finding_key}'",
                     "live_model" if about_model else "recorded")]
    if detector == "step_limit_reached":
        max_steps = int(facts.get("max_steps") or 0)
        if max_steps > 1:
            return [make("max_llm_calls", {"max": max_steps - 1},
                         f"The run finishes in fewer than {max_steps} model calls", "recorded")]
        return []
    if detector in ("tool_error", "timeout"):
        if about_model:
            return [make("detector_absent", {"detector": detector},
                         f"No {detector.replace('_', ' ')} on model calls", "live_model")]
        params: dict[str, Any] = {"detector": detector, **({"tool": target} if target else {})}
        return [make("detector_absent", params,
                     f"'{target or 'the tool'}' no longer fails"
                     + (" by timing out" if detector == "timeout" else ""),
                     # Replaying a recorded error can't make the tool succeed.
                     "live_tools")]
    if detector == "unhandled_exception":
        return [make("no_unhandled_exception", {},
                     "No exception escapes the agent run", "recorded")]
    if detector == "token_budget_exceeded":
        budget = int(facts.get("budget") or config.token_budget_exceeded.max_tokens_per_trace)
        if finding_key == "trace":
            return [make("max_total_tokens", {"max": budget},
                         f"The run uses at most {budget:,} tokens", "live_model")]
        return [make("detector_absent", {"detector": detector},
                     "No model call exceeds the per-call token budget", "live_model")]
    if detector == "high_latency":
        threshold_ms = float(facts.get("threshold_ms") or config.high_latency.max_trace_ms)
        if finding_key == "trace":
            return [make("max_duration_ms", {"max": threshold_ms},
                         f"The run takes at most {threshold_ms:,.0f} ms", "live")]
        scope = {"tool": target} if target else {}
        return [make("detector_absent", {"detector": detector, **scope},
                     f"'{target or finding_key}' stays under its latency threshold", "live")]
    return []


GUARDS: tuple[Assertion, ...] = (
    Assertion(id="", kind="run_completed", params={}, role="guard", requires="recorded",
              description="The run still completes"),
    Assertion(id="", kind="no_unhandled_exception", params={}, role="guard",
              requires="recorded", description="No exception escapes the agent run"),
)


def select(candidates: list[Assertion], source: TraceView,
           config: DetectorConfig) -> tuple[list[Assertion], list[dict[str, str]]]:
    """Keep 'reproduces' assertions that fail on the source and guards that pass on it;
    number them a1, a2, …; report everything dropped and why."""
    kept: list[Assertion] = []
    dropped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.kind, repr(sorted(candidate.params.items())))
        if key in seen:
            continue  # e.g. a guard duplicating a 'reproduces' assertion
        result = evaluate(candidate, source, config)
        if candidate.role == "reproduces" and result.passed:
            dropped.append({"description": candidate.description,
                            "reason": f"passes on the source run ({result.observed}), so it "
                                      "wouldn't detect this failure"})
            continue
        if candidate.role == "guard" and not result.passed:
            dropped.append({"description": candidate.description,
                            "reason": f"already fails on the source run ({result.observed})"})
            continue
        seen.add(key)
        kept.append(candidate.model_copy(update={"id": f"a{len(kept) + 1}"}))
    return kept, dropped
