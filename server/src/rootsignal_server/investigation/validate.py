"""Gatekeeper for claims: references must exist, kinds must suit the source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rootsignal_server.investigation.model import (
    ALLOWED_KINDS,
    NEEDS_REFS,
    Claim,
    EvidenceBundle,
)

MAX_STATEMENT_CHARS = 600


@dataclass(frozen=True)
class Rejected:
    claim: dict[str, Any]
    reason: str


def validate(claims: list[Claim], bundle: EvidenceBundle) -> tuple[list[Claim], list[Rejected]]:
    spans, traces, findings, stats = bundle.known_refs()
    accepted: list[Claim] = []
    rejected: list[Rejected] = []
    seen: set[tuple[str, str]] = set()

    def reject(claim: Claim, reason: str) -> None:
        rejected.append(Rejected(claim.as_dict(), reason))

    for claim in claims:
        if claim.kind not in ALLOWED_KINDS.get(claim.source, frozenset()):
            reject(claim, f"source '{claim.source}' may not assert '{claim.kind}'")
            continue
        statement = claim.statement.strip()
        if not statement:
            reject(claim, "empty statement")
            continue
        if len(statement) > MAX_STATEMENT_CHARS:
            reject(claim, "statement too long")
            continue
        if claim.kind in NEEDS_REFS and not claim.refs:
            reject(claim, f"'{claim.kind}' claims must cite evidence")
            continue
        bad = next((r for r in claim.refs if not _resolves(r, spans, traces, findings, stats)),
                   None)
        if bad is not None:
            reject(claim, f"reference not in evidence: {bad}")
            continue
        key = (claim.kind, statement.lower())
        if key in seen:
            reject(claim, "duplicate")
            continue
        seen.add(key)
        accepted.append(Claim(claim.kind, statement, claim.refs, claim.source))
    return accepted, rejected


def _resolves(ref: Any, spans: set[tuple[str, str]], traces: set[str], findings: set[str],
              stats: set[str]) -> bool:
    if not isinstance(ref, dict):
        return False
    kind = ref.get("type")
    if kind == "span":
        return (ref.get("trace_id"), ref.get("span_id")) in spans
    if kind == "trace":
        return ref.get("trace_id") in traces
    if kind == "finding":
        return ref.get("finding_id") in findings
    if kind == "stat":
        return ref.get("name") in stats
    return False
