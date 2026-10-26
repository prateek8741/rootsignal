"""In-memory trace model that detectors read, and the findings they produce."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property
from typing import Any, Literal

from rootsignal_server.ingest.normalize import NormalizedSpan

Severity = Literal["critical", "high", "medium", "low"]


@dataclass(frozen=True)
class SpanView:
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    status: str
    status_message: str | None
    error_type: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: float | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    tool_name: str | None
    side_effect: str | None
    args_hash: str | None
    call_index: int | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[Mapping[str, Any], ...] = ()
    dialect: str = "rootsignal"

    @property
    def failed(self) -> bool:
        return self.status == "error"

    @property
    def is_tool(self) -> bool:
        return self.kind in ("tool", "mcp")

    @property
    def has_exception_event(self) -> bool:
        return any(e.get("name") == "exception" for e in self.events)

    @property
    def target(self) -> str:
        """What the span acted on: tool name, model, or span name."""
        return self.tool_name or self.model or self.name

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> SpanView:
        values: dict[str, Any] = {f: row.get(f) for f in _SPAN_FIELDS
                                  if f not in ("attributes", "events", "dialect")}
        return cls(**values, attributes=row.get("attributes") or {},
                   events=tuple(row.get("events") or ()),
                   dialect=row.get("dialect") or "rootsignal")

    @classmethod
    def from_normalized(cls, span: NormalizedSpan) -> SpanView:
        return cls.from_mapping(vars(span))


_SPAN_FIELDS = tuple(SpanView.__dataclass_fields__)


def shadowed_ids(spans: Iterable[SpanView]) -> set[str]:
    """Third-party tool spans that duplicate one of our SDK's tool spans.

    With OpenInference and ``@rs.tool`` on the same function, every call yields
    two tool spans. The SDK span is kept (it knows the side effect and hashes the
    raw arguments); the other is shadowed when an SDK span for the same tool
    starts inside it. Parent links can't be used: OpenInference's LangChain
    tracer doesn't make its spans current, so the SDK span isn't its child.
    """
    spans = list(spans)
    ours = [s for s in spans if s.is_tool and s.dialect == "rootsignal"]
    out: set[str] = set()
    for span in spans:
        if not span.is_tool or span.dialect == "rootsignal":
            continue
        end = span.ended_at or span.started_at
        if any(r.tool_name == span.tool_name and span.started_at <= r.started_at <= end
               for r in ours):
            out.add(span.span_id)
    return out


def drop_shadowed(spans: list[SpanView]) -> list[SpanView]:
    """Remove shadowed spans; their children move up to the shadowed span's parent."""
    shadowed = shadowed_ids(spans)
    if not shadowed:
        return spans
    parent_of = {s.span_id: s.parent_span_id for s in spans}

    def lift(parent: str | None) -> str | None:
        while parent in shadowed:
            parent = parent_of[parent]
        return parent

    return [replace(s, parent_span_id=lift(s.parent_span_id)) for s in spans
            if s.span_id not in shadowed]


@dataclass(frozen=True)
class TraceView:
    trace_id: str
    spans: tuple[SpanView, ...]

    @classmethod
    def build(cls, trace_id: str, spans: Iterable[SpanView]) -> TraceView:
        kept = drop_shadowed(list(spans))
        ordered = sorted(kept, key=lambda s: (s.started_at, s.call_index or 0, s.span_id))
        return cls(trace_id, tuple(ordered))

    @cached_property
    def by_id(self) -> dict[str, SpanView]:
        return {s.span_id: s for s in self.spans}

    @cached_property
    def roots(self) -> tuple[SpanView, ...]:
        return tuple(s for s in self.spans
                     if s.parent_span_id is None or s.parent_span_id not in self.by_id)

    @cached_property
    def agents(self) -> tuple[SpanView, ...]:
        return tuple(s for s in self.spans if s.kind == "agent")

    @property
    def agent_name(self) -> str | None:
        return self.agents[0].attributes.get("gen_ai.agent.name") if self.agents else None

    @property
    def agent_version(self) -> str | None:
        return self.agents[0].attributes.get("gen_ai.agent.version") if self.agents else None

    def ancestors(self, span: SpanView) -> list[SpanView]:
        out: list[SpanView] = []
        seen: set[str] = set()
        current = span
        while current.parent_span_id and current.parent_span_id in self.by_id:
            if current.parent_span_id in seen:  # defensive: malformed cyclic input
                break
            seen.add(current.parent_span_id)
            current = self.by_id[current.parent_span_id]
            out.append(current)
        return out

    def nearest_agent(self, span: SpanView) -> SpanView | None:
        return next((a for a in self.ancestors(span) if a.kind == "agent"), None)

    def duration_ms(self) -> float | None:
        if not self.spans:
            return None
        if len(self.roots) == 1 and self.roots[0].duration_ms is not None:
            return self.roots[0].duration_ms
        ends = [s.ended_at for s in self.spans if s.ended_at]
        if not ends:
            return None
        return (max(ends) - self.spans[0].started_at).total_seconds() * 1000


@dataclass(frozen=True)
class Finding:
    detector: str
    detector_version: int
    key: str
    severity: Severity
    title: str
    summary: str
    evidence_span_ids: tuple[str, ...]
    facts: Mapping[str, Any] = field(default_factory=dict)
    tool_name: str | None = None
    error_type: str | None = None
