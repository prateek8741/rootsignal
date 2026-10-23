"""Evidence bundles and claims.

A claim is a typed statement with references into the evidence bundle:

* ``observed``: a fact read off the data. Produced only by deterministic rules.
* ``hypothesis`` / ``alternative``: a possible cause, and a competing one.
  Must cite the evidence it rests on.
* ``missing_evidence``: what we'd need to tell hypotheses apart.
* ``next_step``: what to do next.
* ``verified``: reserved for a passing replay (increment 8); never produced here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ClaimKind = Literal["observed", "hypothesis", "alternative", "missing_evidence", "next_step",
                    "verified"]
ClaimSource = Literal["rules", "llm", "replay"]
KIND_ORDER: tuple[str, ...] = ("observed", "hypothesis", "alternative", "missing_evidence",
                               "next_step", "verified")
# What each source is allowed to assert.
ALLOWED_KINDS: dict[str, frozenset[str]] = {
    "rules": frozenset({"observed", "hypothesis", "alternative", "missing_evidence", "next_step"}),
    "llm": frozenset({"hypothesis", "alternative", "missing_evidence", "next_step"}),
    "replay": frozenset({"verified"}),
}
NEEDS_REFS = frozenset({"observed", "hypothesis", "alternative", "verified"})


@dataclass(frozen=True)
class Claim:
    kind: str
    statement: str
    refs: tuple[dict[str, str], ...] = ()
    source: str = "rules"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "statement": self.statement, "refs": list(self.refs),
                "source": self.source}


def span_ref(trace_id: str, span_id: str) -> dict[str, str]:
    return {"type": "span", "trace_id": trace_id, "span_id": span_id}


def trace_ref(trace_id: str) -> dict[str, str]:
    return {"type": "trace", "trace_id": trace_id}


def finding_ref(finding_id: str, trace_id: str) -> dict[str, str]:
    """``trace_id`` lets readers link to where the finding lives; validation uses the ID."""
    return {"type": "finding", "finding_id": finding_id, "trace_id": trace_id}


def stat_ref(name: str) -> dict[str, str]:
    return {"type": "stat", "name": name}


@dataclass
class SpanEvidence:
    span_id: str
    kind: str
    name: str
    tool_name: str | None
    model: str | None
    status: str
    error_type: str | None
    status_message: str | None
    duration_ms: float | None
    repeat_index: int | None
    side_effect: str | None
    input_captured: bool


@dataclass
class FindingEvidence:
    finding_id: str
    detector: str
    title: str
    summary: str
    evidence_span_ids: list[str]
    facts: dict[str, Any]


@dataclass
class TraceEvidence:
    trace_id: str
    roles: list[str]  # first / latest / worst
    agent_name: str | None
    agent_version: str | None
    started_at: str
    status: str
    completion_reason: str | None
    spans: list[SpanEvidence]
    findings: list[FindingEvidence]
    spans_truncated: bool = False


@dataclass
class EvidenceBundle:
    issue: dict[str, Any]
    traces: list[TraceEvidence]
    stats: dict[str, Any] = field(default_factory=dict)

    def known_refs(self) -> tuple[set[tuple[str, str]], set[str], set[str], set[str]]:
        spans = {(t.trace_id, s.span_id) for t in self.traces for s in t.spans}
        traces = {t.trace_id for t in self.traces}
        findings = {f.finding_id for t in self.traces for f in t.findings}
        return spans, traces, findings, set(self.stats)

    def by_role(self, role: str) -> TraceEvidence | None:
        return next((t for t in self.traces if role in t.roles), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceBundle:
        traces = [TraceEvidence(
            **{**t, "spans": [SpanEvidence(**s) for s in t["spans"]],
               "findings": [FindingEvidence(**f) for f in t["findings"]]})
            for t in data["traces"]]
        return cls(issue=data["issue"], traces=traces, stats=data.get("stats", {}))
