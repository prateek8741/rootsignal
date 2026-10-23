"""Replay verdicts and case status transitions. Pure functions.

Verdict, given which assertions the replay mode can evaluate:

* ``diverged``: the run couldn't stay faithful to the recording (unmatched
  call, unsupported interception, blocked network). Nothing is concluded.
* ``inconclusive``: no enabled 'reproduces' assertion can be evaluated in
  this mode (e.g. it needs live tools).
* ``broken``: a guard failed (the run crashed or didn't complete).
* ``failing``: a 'reproduces' assertion failed: the failure is present.
* ``passing``: every evaluated assertion passed: the failure is absent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.model import TraceView
from rootsignal_server.regression.assertions import evaluate
from rootsignal_server.regression.spec import Assertion

EVALUABLE = {"recorded": {"recorded"}, "live_model": {"recorded", "live_model"}}
EVER_REPRODUCED = {"reproduced", "passing", "failing"}


@dataclass(frozen=True)
class Outcome:
    verdict: str
    results: list[dict[str, Any]]
    summary: str


def judge(assertions: list[Assertion], trace: TraceView, *, mode: str,
          report: Mapping[str, Any], config: DetectorConfig) -> Outcome:
    results: list[dict[str, Any]] = []
    for assertion in assertions:
        row: dict[str, Any] = {"id": assertion.id, "kind": assertion.kind, "role": assertion.role,
                               "requires": assertion.requires,
                               "description": assertion.description}
        if not assertion.enabled:
            results.append({**row, "evaluated": False, "reason": "disabled"})
        elif assertion.requires not in EVALUABLE[mode]:
            results.append({**row, "evaluated": False,
                            "reason": f"needs {assertion.requires.replace('_', ' ')}; this "
                                      f"replay ran in {mode} mode"})
        else:
            result = evaluate(assertion, trace, config)
            results.append({**row, "evaluated": True, "passed": result.passed,
                            "observed": result.observed})

    problems = [*report.get("divergences", []), *report.get("unsupported", []),
                *[f"blocked network access to {b}" for b in report.get("blocked", [])]]
    if problems:
        return Outcome("diverged", results, "Replay diverged from the recording: "
                       + "; ".join(problems[:3]))
    evaluated = [r for r in results if r["evaluated"]]
    reproducing = [r for r in evaluated if r["role"] == "reproduces"]
    if not reproducing:
        return Outcome("inconclusive", results, "No enabled 'reproduces' assertion can be "
                       f"evaluated in {mode} mode.")
    broken = [r for r in evaluated if r["role"] == "guard" and not r["passed"]]
    if broken:
        return Outcome("broken", results, "A guard failed: " + broken[0]["observed"])
    failing = [r for r in reproducing if not r["passed"]]
    if failing:
        return Outcome("failing", results,
                       f"Failure present: {failing[0]['description']} ({failing[0]['observed']}).")
    return Outcome("passing", results,
                   f"Failure absent: all {len(evaluated)} evaluated assertion(s) passed.")


def next_case_status(current: str, purpose: str, verdict: str) -> tuple[str, str | None]:
    """Return (new status, note). A case only counts as passing after it has reproduced."""
    if purpose == "reproduce":
        if verdict == "failing":
            return "reproduced", None
        if verdict == "passing":
            return "not_reproduced", ("Replay against this code didn't reproduce the failure; "
                                      "the case can't prove a fix until it does.")
        return current, None
    if verdict == "passing":
        if current in EVER_REPRODUCED:
            return "passing", None
        return current, ("Passed, but this case has never reproduced the failure, so the "
                         "pass doesn't show that a bug was fixed.")
    if verdict == "failing" and current in EVER_REPRODUCED:
        return "failing", None
    return current, None
