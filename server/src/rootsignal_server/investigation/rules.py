"""Deterministic investigation rules: evidence bundle → claims.

Observed claims only restate what the bundle contains (counts, quotes,
statistics). Hypotheses name a possible cause and cite what supports it;
where the data can't separate two explanations, the rules say so with a
``missing_evidence`` claim instead of picking one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rootsignal_server.investigation.model import (
    Claim,
    EvidenceBundle,
    finding_ref,
    span_ref,
    stat_ref,
    trace_ref,
)

# A dependency counts as "failing for everyone" / "fine for others" at these rates.
OUTAGE_ERROR_RATE = 0.5
HEALTHY_ERROR_RATE = 0.2
MIN_CALLS_FOR_COMPARISON = 3
MIN_TRACES_FOR_VERSION_CONTRAST = 3


def _short(trace_id: str) -> str:
    return trace_id[:8]


def _when(iso: str) -> str:
    """'2026-10-23T09:16:47.219311+00:00' → '2026-10-23 09:16 UTC' (readable in claims)."""
    try:
        moment = datetime.fromisoformat(iso).astimezone(UTC)
    except ValueError:
        return iso
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "n/a"


def investigate(bundle: EvidenceBundle) -> list[Claim]:
    claims: list[Claim] = []
    claims += _occurrence_facts(bundle)
    claims += _version_claims(bundle)
    claims += _detector_claims(bundle)
    claims += _missing_evidence(bundle)
    claims += _next_steps(bundle)
    return claims


# --- observed facts ---------------------------------------------------------------

def _occurrence_facts(bundle: EvidenceBundle) -> list[Claim]:
    issue = bundle.issue
    claims = [Claim(
        "observed",
        f"The issue has occurred in {issue['occurrence_count']} trace(s) between "
        f"{_when(issue['first_seen_at'])} and {_when(issue['last_seen_at'])}"
        + (f"; it has regressed {issue['regression_count']} time(s)."
           if issue["regression_count"] else "."),
        tuple(trace_ref(t.trace_id) for t in bundle.traces))]
    for trace in bundle.traces:
        for finding in trace.findings:
            refs = (finding_ref(finding.finding_id, trace.trace_id),
                    *(span_ref(trace.trace_id, s) for s in finding.evidence_span_ids[:6]))
            claims.append(Claim(
                "observed",
                f"In trace {_short(trace.trace_id)} ({'/'.join(trace.roles)}): {finding.summary}",
                refs))
    worst = bundle.by_role("worst") or (bundle.traces[0] if bundle.traces else None)
    if worst:
        seen: set[str] = set()
        for span in worst.spans:
            if span.status != "error" or (span.error_type or "") in seen:
                continue
            seen.add(span.error_type or "")
            message = f': "{span.status_message}"' if span.status_message else ""
            claims.append(Claim(
                "observed",
                f"In trace {_short(worst.trace_id)}, {span.kind} span "
                f"'{span.tool_name or span.model or span.name}' failed with "
                f"{span.error_type or 'an error'}{message}.",
                (span_ref(worst.trace_id, span.span_id),)))
            if len(seen) >= 3:
                break
    return claims


def _version_claims(bundle: EvidenceBundle) -> list[Claim]:
    versions = [v for v in bundle.stats.get("version_breakdown", []) if v["agent_version"]]
    if not versions:
        return []
    ref = (stat_ref("version_breakdown"),)
    breakdown = "; ".join(
        f"{v['agent_version']}: {v['affected']} of {v['traces']} traces "
        f"({_pct(v['affected'], v['traces'])})" for v in versions)
    claims = [Claim("observed", f"Traces of this agent in the window, by version: {breakdown}.",
                    ref)]
    affected = [v for v in versions if v["affected"] > 0]
    clean = [v for v in versions
             if v["affected"] == 0 and v["traces"] >= MIN_TRACES_FOR_VERSION_CONTRAST]
    if affected and clean:
        a = ", ".join(v["agent_version"] for v in affected)
        c = ", ".join(v["agent_version"] for v in clean)
        claims.append(Claim(
            "hypothesis",
            f"The failure depends on the agent version: it appears in {a} but not in {c}, "
            "which ran in the same window.", ref))
        claims.append(Claim(
            "alternative",
            f"{c} may receive different traffic (other users, inputs or routes) rather than "
            "behave differently.", ref))
    elif len(affected) == len(versions) and len(versions) > 1:
        claims.append(Claim(
            "hypothesis",
            "Every agent version seen in the window is affected, so the cause is more likely "
            "outside the agent's recent changes (a dependency, data or configuration).", ref))
    return claims


# --- detector-specific hypotheses -----------------------------------------------------

def _detector_claims(bundle: EvidenceBundle) -> list[Claim]:
    detector = bundle.issue["detector"]
    claims: list[Claim] = []
    if bundle.issue.get("tool_name") and detector in (
            "tool_error", "timeout", "repeated_tool_call", "excessive_retries"):
        claims += _dependency_claims(bundle)
    worst = bundle.by_role("worst")
    finding_refs = tuple(finding_ref(f.finding_id, t.trace_id)
                         for t in bundle.traces for f in t.findings)
    if not finding_refs:
        return claims
    if detector in ("repeated_tool_call", "excessive_retries"):
        failed = any(f.facts.get("failed_calls") or f.facts.get("failed_attempts")
                     for t in bundle.traces for f in t.findings)
        if failed:
            claims.append(Claim(
                "hypothesis",
                "The agent re-issues the identical call after it fails instead of handling "
                "the error (no retry limit, backoff or fallback).", finding_refs))
        else:
            claims.append(Claim(
                "hypothesis",
                "The agent repeats an identical call whose result doesn't move it forward "
                "(for example, an empty search result it keeps retrying).", finding_refs))
            claims.append(Claim(
                "alternative",
                "The repetition may be intentional (polling or cheap cached reads); if so, "
                "this is a detector false positive for this tool.", finding_refs))
    elif detector == "step_limit_reached":
        errors_in_run = [s for s in (worst.spans if worst else []) if s.status == "error"]
        refs = finding_refs
        if worst:
            refs += tuple(span_ref(worst.trace_id, s.span_id) for s in errors_in_run[:5])
        claims.append(Claim(
            "hypothesis",
            "The run spends its whole step budget without making progress"
            + (f"; {len(errors_in_run)} span(s) in the worst run failed, so the loop is "
               "probably driven by those failures." if errors_in_run else "."), refs))
        claims.append(Claim(
            "alternative",
            "The task may legitimately need more steps than the configured max_steps.",
            finding_refs))
    elif detector == "unhandled_exception":
        error = bundle.issue.get("error_type") or "the exception"
        origin = bundle.issue.get("tool_name")
        claims.append(Claim(
            "hypothesis",
            f"{error} raised in {f'tool {origin!r}' if origin else 'agent code'} is not "
            "handled anywhere on the path to the run's entry point.", finding_refs))
        claims.append(Claim(
            "alternative",
            "Inputs or upstream data changed shape so that a previously unreachable error "
            "path is now taken.", finding_refs))
    elif detector == "token_budget_exceeded":
        claims.append(Claim(
            "hypothesis",
            "Context grows across steps (conversation history or retrieved documents "
            "appended without trimming).", finding_refs))
        claims.append(Claim(
            "alternative", "A few unusually large inputs drive the token count, not growth.",
            finding_refs))
    elif detector == "high_latency" and worst:
        slowest = max((s for s in worst.spans if s.kind != "agent" and s.duration_ms),
                      key=lambda s: s.duration_ms or 0, default=None)
        if slowest:
            target = slowest.tool_name or slowest.model or slowest.name
            claims.append(Claim(
                "hypothesis",
                f"Latency is dominated by {slowest.kind} '{target}' "
                f"({slowest.duration_ms or 0:,.0f} ms in the worst run).",
                (span_ref(worst.trace_id, slowest.span_id), *finding_refs)))
    return claims


def _dependency_claims(bundle: EvidenceBundle) -> list[Claim]:
    tool = bundle.issue["tool_name"]
    agent = bundle.issue.get("agent_name")
    callers = bundle.stats.get("tool_callers", [])
    ref = (stat_ref("tool_callers"),)
    if not callers:
        return []
    summary = "; ".join(f"{c['agent_name'] or 'unknown agent'}: {c['errors']} of {c['calls']} "
                        f"calls failed" for c in callers)
    claims = [Claim("observed", f"Calls to '{tool}' in the window by agent: {summary}.", ref)]
    others = [c for c in callers if c["agent_name"] != agent]
    other_calls = sum(c["calls"] for c in others)
    other_errors = sum(c["errors"] for c in others)
    if other_calls < MIN_CALLS_FOR_COMPARISON:
        return claims  # can't compare: covered by a missing_evidence claim
    rate = other_errors / other_calls
    if rate >= OUTAGE_ERROR_RATE:
        claims.append(Claim(
            "hypothesis",
            f"'{tool}' itself was failing in this window: other agents saw {other_errors} of "
            f"{other_calls} calls fail too, so an outage or degradation is more likely than "
            f"a problem in {agent or 'this agent'}.", ref))
        claims.append(Claim(
            "alternative",
            f"All callers may send '{tool}' the same kind of bad input (shared client or "
            "library change).", ref))
    elif rate <= HEALTHY_ERROR_RATE:
        claims.append(Claim(
            "hypothesis",
            f"The failure is specific to how {agent or 'this agent'} calls '{tool}' (arguments, "
            f"configuration or credentials): other agents' calls mostly succeeded "
            f"({other_errors} of {other_calls} failed).", ref))
        claims.append(Claim(
            "alternative",
            f"'{tool}' may fail intermittently and these runs were unlucky.", ref))
    return claims


# --- gaps and next steps ---------------------------------------------------------------

def _missing_evidence(bundle: EvidenceBundle) -> list[Claim]:
    claims: list[Claim] = []
    tool = bundle.issue.get("tool_name")
    agent = bundle.issue.get("agent_name")
    if tool:
        others = [c for c in bundle.stats.get("tool_callers", []) if c["agent_name"] != agent]
        if sum(c["calls"] for c in others) < MIN_CALLS_FOR_COMPARISON:
            claims.append(Claim(
                "missing_evidence",
                f"No other agent called '{tool}' in this window, so an outage can't be told "
                f"apart from a problem specific to {agent or 'this agent'}."))
        if bundle.issue["detector"] in ("tool_error", "timeout"):
            claims.append(Claim(
                "missing_evidence",
                f"Service-side logs or metrics for '{tool}' in the window "
                f"({_when(bundle.stats.get('window', {}).get('start', '?'))} to "
                f"{_when(bundle.stats.get('window', {}).get('end', '?'))})."))
    partial = [t for t in bundle.traces if t.completion_reason == "idle_timeout"]
    if partial:
        claims.append(Claim(
            "missing_evidence",
            "Trace(s) " + ", ".join(_short(t.trace_id) for t in partial)
            + " are partial: the root span never arrived."))
    uncaptured = [(t, s) for t in bundle.traces for s in t.spans
                  if s.status == "error" and s.kind in ("tool", "mcp") and not s.input_captured]
    if uncaptured:
        claims.append(Claim(
            "missing_evidence",
            "Inputs of failing tool calls weren't captured (payload capture is off), so their "
            "arguments can't be inspected."))
    if any(t.spans_truncated for t in bundle.traces):
        claims.append(Claim(
            "missing_evidence", "Some traces have more spans than the investigation reads."))
    return claims


def _next_steps(bundle: EvidenceBundle) -> list[Claim]:
    worst = bundle.by_role("worst")
    steps = []
    detector = bundle.issue["detector"]
    if detector in ("repeated_tool_call", "excessive_retries", "step_limit_reached"):
        steps.append("Cap identical retries and give the agent an explicit failure path "
                     "(fallback, escalation, or an honest error to the user).")
    if detector in ("tool_error", "timeout") and bundle.issue.get("tool_name"):
        steps.append(f"Check the health and recent changes of '{bundle.issue['tool_name']}' "
                     "for the window above.")
    if worst:
        steps.append(f"Turn the worst trace ({_short(worst.trace_id)}) into a regression case "
                     "and confirm it fails before the fix and passes after.")
    return [Claim("next_step", s) for s in steps]


def summarize(claims: list[Claim], bundle: EvidenceBundle) -> str:
    hypotheses = [c for c in claims if c.kind == "hypothesis"]
    lead = f" Leading hypothesis: {hypotheses[0].statement}" if hypotheses else ""
    return (f"{bundle.issue['occurrence_count']} trace(s), {len(hypotheses)} "
            f"hypothesis(es) to check.{lead}")

