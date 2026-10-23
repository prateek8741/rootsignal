"""Deterministic failure detectors.

Each detector is a pure function ``(TraceView, DetectorConfig) -> list[Finding]``.
Findings state observations only (what happened, counts, span IDs); causes
are the investigation step's job. Bump a detector's version whenever its
logic changes, so findings stay attributable to the code that produced them.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.model import Finding, Severity, SpanView, TraceView

_TIMEOUT_TYPE = re.compile(r"time[d_ ]?out|deadline", re.IGNORECASE)
# Class names are CamelCase (ReadTimeoutError), so no word boundaries there; messages
# need them so that e.g. "invalid field 'timeout_seconds'" isn't read as a timeout.
_TIMEOUT_MESSAGE = re.compile(r"\btimed? ?out\b|\bdeadline exceeded\b", re.IGNORECASE)


def is_timeout(span: SpanView) -> bool:
    if not span.failed:
        return False
    if span.error_type and _TIMEOUT_TYPE.search(span.error_type):
        return True
    return bool(span.status_message and _TIMEOUT_MESSAGE.search(span.status_message))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------------------

def unhandled_exception(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    """The agent run itself ended with an exception (it escaped the agent)."""
    candidates = [a for a in trace.agents if a.failed and a.has_exception_event]
    if not trace.agents:
        # Only a true root stands in for the run. An orphan (its parent never
        # arrived) is just a span whose context we lost, not the run itself.
        candidates = [r for r in trace.roots
                      if r.parent_span_id is None and r.failed and r.has_exception_event]
    # Report the outermost failure once; nested agents re-raise the same error.
    outermost = [c for c in candidates
                 if not any(a in candidates for a in trace.ancestors(c))]
    findings = []
    for run in outermost:
        error_type = run.error_type or "error"
        origins = [s for s in trace.spans if s.failed and s.error_type == error_type
                   and s.has_exception_event and run in trace.ancestors(s)]
        # Where it was first raised: the deepest span that recorded it (earliest on ties);
        # intermediate agents that re-raised it are not the origin.
        origin = (min(origins, key=lambda s: (-len(trace.ancestors(s)), s.started_at))
                  if origins else None)
        evidence = (run.span_id, *((origin.span_id,) if origin else ()))
        where = f"; first raised in {origin.kind} span '{origin.name}'" if origin else ""
        findings.append(Finding(
            "unhandled_exception", 1, run.span_id, "high",
            f"Agent run '{run.name}' ended with {error_type}",
            f"The run span ended with status error and an exception event ({error_type}){where}.",
            evidence,
            {"error_type": error_type, "origin_span_id": origin.span_id if origin else None},
            tool_name=origin.tool_name if origin else None, error_type=error_type))
    return findings


def tool_error(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    """Tool calls that failed for reasons other than timeouts (see ``timeout``)."""
    groups: dict[tuple[str, str], list[SpanView]] = defaultdict(list)
    calls: dict[str, int] = defaultdict(int)
    for span in trace.spans:
        if span.is_tool:
            calls[span.target] += 1
            if span.failed and not is_timeout(span):
                groups[(span.target, span.error_type or "error")].append(span)
    findings = []
    for (tool, error_type), spans in groups.items():
        findings.append(Finding(
            "tool_error", 1, f"{tool}:{error_type}", "medium",
            f"Tool '{tool}' failed with {error_type}",
            f"{_plural(len(spans), 'call')} to '{tool}' failed with {error_type} "
            f"out of {_plural(calls[tool], 'call')} in this trace.",
            tuple(s.span_id for s in spans),
            {"failed_calls": len(spans), "total_calls": calls[tool],
             "side_effect": spans[0].side_effect},
            tool_name=tool, error_type=error_type))
    return findings


def timeout(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    groups: dict[tuple[str, str], list[SpanView]] = defaultdict(list)
    for span in trace.spans:
        if is_timeout(span) and span.kind != "agent":
            groups[(span.kind, span.target)].append(span)
    findings = []
    for (kind, target), spans in groups.items():
        error_type = spans[0].error_type or "timeout"
        findings.append(Finding(
            "timeout", 1, f"{kind}:{target}", "medium",
            f"{kind.capitalize()} '{target}' timed out",
            f"{_plural(len(spans), kind + ' span')} for '{target}' failed with a timeout "
            f"({error_type}).",
            tuple(s.span_id for s in spans),
            {"count": len(spans), "kind": kind,
             "durations_ms": [s.duration_ms for s in spans]},
            tool_name=target if kind in ("tool", "mcp") else None, error_type=error_type))
    return findings


def _identical_tool_calls(trace: TraceView) -> dict[tuple[str, str], list[SpanView]]:
    groups: dict[tuple[str, str], list[SpanView]] = defaultdict(list)
    for span in trace.spans:
        if span.is_tool and span.args_hash:
            groups[(span.target, span.args_hash)].append(span)
    return groups


def repeated_tool_call(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    """The same tool called with identical arguments several times in one trace."""
    threshold = config.repeated_tool_call.min_calls
    findings = []
    for (tool, args_hash), spans in _identical_tool_calls(trace).items():
        if len(spans) < threshold:
            continue
        failed = sum(s.failed for s in spans)
        effect = spans[0].side_effect
        severity: Severity = "high" if effect == "write" else "medium"
        findings.append(Finding(
            "repeated_tool_call", 1, f"{tool}:{args_hash}", severity,
            f"'{tool}' called {len(spans)} times with identical arguments",
            f"'{tool}' ran {_plural(len(spans), 'time')} with the same arguments "
            f"(args hash {args_hash}); {failed} of those calls failed. "
            f"Declared side effect: {effect or 'unknown'}.",
            tuple(s.span_id for s in spans),
            {"calls": len(spans), "failed_calls": failed, "args_hash": args_hash,
             "side_effect": effect, "threshold": threshold},
            tool_name=tool, error_type=spans[0].error_type))
    return findings


def excessive_retries(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    """Identical attempts after a failure: of a tool call, or of a model call."""
    threshold = config.excessive_retries.min_retries
    sequences: dict[str, list[SpanView]] = {
        f"tool:{tool}:{h}": spans for (tool, h), spans in _identical_tool_calls(trace).items()}
    for span in trace.spans:
        if span.kind == "llm" and span.model:
            sequences.setdefault(f"llm:{span.model}", []).append(span)
    findings = []
    for key, spans in sequences.items():
        streak = _longest_retry_streak(spans)
        if streak is None or len(streak) - 1 < threshold:
            continue
        retries = len(streak) - 1
        recovered = not streak[-1].failed
        target = spans[0].target
        findings.append(Finding(
            "excessive_retries", 1, key, "low" if recovered else "medium",
            f"'{target}' retried {retries} times after failing",
            f"After a failed call to '{target}', {_plural(retries, 'attempt')} followed "
            f"back to back; the last one {'succeeded' if recovered else 'failed'}.",
            tuple(s.span_id for s in streak),
            {"retries": retries, "recovered": recovered, "threshold": threshold,
             "failed_attempts": sum(s.failed for s in streak)},
            tool_name=target if spans[0].is_tool else None,
            error_type=streak[0].error_type))
    return findings


def _longest_retry_streak(spans: list[SpanView]) -> list[SpanView] | None:
    """Longest run that starts with a failure and continues while attempts keep
    following a failed attempt. A call made after a success is not a retry."""
    best: list[SpanView] | None = None
    current: list[SpanView] | None = None
    for span in spans:
        if current is not None and current[-1].failed:
            current.append(span)
        elif span.failed:
            current = [span]
        else:
            current = None
        if current is not None and (best is None or len(current) > len(best)):
            best = list(current)
    return best


def step_limit_reached(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    """The agent used all of its configured model-call steps (``max_steps``)."""
    findings = []
    for agent in trace.agents:
        max_steps = agent.attributes.get("rootsignal.run.max_steps")
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps <= 0:
            continue
        steps = [s for s in trace.spans if s.kind == "llm" and trace.nearest_agent(s) is agent]
        if len(steps) < max_steps:
            continue
        name = agent.attributes.get("gen_ai.agent.name") or agent.name
        findings.append(Finding(
            "step_limit_reached", 1, agent.span_id, "high",
            f"Agent '{name}' used all {max_steps} steps",
            f"The run made {_plural(len(steps), 'model call')} against a limit of {max_steps}.",
            (agent.span_id, *(s.span_id for s in steps)),
            {"steps": len(steps), "max_steps": max_steps}))
    return findings


def token_budget_exceeded(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    cfg = config.token_budget_exceeded
    llm = [s for s in trace.spans if s.kind == "llm"]

    def tokens(s: SpanView) -> int:
        return (s.input_tokens or 0) + (s.output_tokens or 0)

    findings = []
    total = sum(tokens(s) for s in llm)
    if total > cfg.max_tokens_per_trace:
        findings.append(Finding(
            "token_budget_exceeded", 1, "trace", "medium",
            f"Trace used {total:,} tokens",
            f"{_plural(len(llm), 'model call')} used {total:,} tokens in total, "
            f"above the per-trace budget of {cfg.max_tokens_per_trace:,}.",
            tuple(s.span_id for s in llm),
            {"tokens": total, "budget": cfg.max_tokens_per_trace}))
    large = [s for s in llm if tokens(s) > cfg.max_tokens_per_call]
    if large:
        findings.append(Finding(
            "token_budget_exceeded", 1, "calls", "medium",
            f"{_plural(len(large), 'model call')} over {cfg.max_tokens_per_call:,} tokens",
            f"Largest call used {max(tokens(s) for s in large):,} tokens; "
            f"per-call budget is {cfg.max_tokens_per_call:,}.",
            tuple(s.span_id for s in large),
            {"calls": len(large), "max_call_tokens": max(tokens(s) for s in large),
             "budget": cfg.max_tokens_per_call}))
    return findings


def high_latency(trace: TraceView, config: DetectorConfig) -> list[Finding]:
    cfg = config.high_latency
    findings = []
    duration = trace.duration_ms()
    if duration is not None and duration > cfg.max_trace_ms:
        findings.append(Finding(
            "high_latency", 1, "trace", "low",
            f"Trace took {duration / 1000:.1f}s",
            f"End-to-end duration {duration:,.0f} ms exceeds {cfg.max_trace_ms:,.0f} ms.",
            tuple(r.span_id for r in trace.roots),
            {"duration_ms": duration, "threshold_ms": cfg.max_trace_ms}))
    limits = {"tool": cfg.max_tool_ms, "mcp": cfg.max_tool_ms, "llm": cfg.max_llm_ms}
    slow: dict[tuple[str, str], list[SpanView]] = defaultdict(list)
    for span in trace.spans:
        limit = limits.get(span.kind)
        if limit is not None and span.duration_ms is not None and span.duration_ms > limit:
            slow[(span.kind, span.target)].append(span)
    for (kind, target), spans in slow.items():
        worst = max(s.duration_ms or 0 for s in spans)
        findings.append(Finding(
            "high_latency", 1, f"{kind}:{target}", "low",
            f"Slow {kind} '{target}' ({worst / 1000:.1f}s)",
            f"{_plural(len(spans), kind + ' span')} for '{target}' exceeded "
            f"{limits[kind]:,.0f} ms; slowest {worst:,.0f} ms.",
            tuple(s.span_id for s in spans),
            {"count": len(spans), "max_duration_ms": worst, "threshold_ms": limits[kind]},
            tool_name=target if kind in ("tool", "mcp") else None))
    return findings


# ---------------------------------------------------------------------------

DetectorFn = Callable[[TraceView, DetectorConfig], list[Finding]]


@dataclass(frozen=True)
class DetectorSpec:
    name: str
    version: int
    fn: DetectorFn


DETECTORS: tuple[DetectorSpec, ...] = (
    DetectorSpec("unhandled_exception", 1, unhandled_exception),
    DetectorSpec("tool_error", 1, tool_error),
    DetectorSpec("timeout", 1, timeout),
    DetectorSpec("repeated_tool_call", 1, repeated_tool_call),
    DetectorSpec("excessive_retries", 1, excessive_retries),
    DetectorSpec("step_limit_reached", 1, step_limit_reached),
    DetectorSpec("token_budget_exceeded", 1, token_budget_exceeded),
    DetectorSpec("high_latency", 1, high_latency),
)
DETECTOR_NAMES = tuple(d.name for d in DETECTORS)


def run_detectors(trace: TraceView, config: DetectorConfig | None = None) -> list[Finding]:
    config = config or DetectorConfig()
    findings: list[Finding] = []
    for spec in DETECTORS:
        if getattr(config, spec.name).enabled:
            findings.extend(spec.fn(trace, config))
    return findings
