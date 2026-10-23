"""Issue fingerprints: which findings count as "the same problem".

A fingerprint hashes a per-detector list of components. Defaults below;
per-project overrides in ``projects.issue_grouping``::

    {"tool_error": ["detector", "agent_name", "tool_name"]}

Overrides must include ``detector`` so different detectors never merge.
Changing an override affects findings grouped afterwards; run
``rootsignal-server regroup-issues`` to re-group existing findings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# subject = tool name when the finding is about a tool, else the finding key
# (e.g. "llm:<model>" or "trace"), which names what the detector measured.
COMPONENTS = ("detector", "agent_name", "agent_version", "tool_name", "error_type", "subject")

DEFAULT_COMPONENTS: Mapping[str, tuple[str, ...]] = {
    "unhandled_exception": ("detector", "agent_name", "error_type", "tool_name"),
    "tool_error": ("detector", "agent_name", "tool_name", "error_type"),
    "timeout": ("detector", "agent_name", "subject"),
    # Not the args hash: different customers' arguments would split one bug.
    "repeated_tool_call": ("detector", "agent_name", "tool_name"),
    "excessive_retries": ("detector", "agent_name", "subject"),
    # Not the finding key: it is the agent span ID, unique per trace.
    "step_limit_reached": ("detector", "agent_name"),
    "token_budget_exceeded": ("detector", "agent_name", "subject"),
    "high_latency": ("detector", "agent_name", "subject"),
}

_DESCRIPTIONS: Mapping[str, str] = {
    "unhandled_exception": "Agent runs end with an exception that escaped the agent.",
    "tool_error": "A tool call fails (other than by timing out).",
    "timeout": "Calls fail because they time out.",
    "repeated_tool_call": "The agent calls the same tool with identical arguments several "
                          "times in one run.",
    "excessive_retries": "The same call is retried back to back after failing.",
    "step_limit_reached": "Agent runs use up their whole step budget.",
    "token_budget_exceeded": "Runs or individual model calls exceed the token budget.",
    "high_latency": "Runs or calls exceed the latency threshold.",
}


@dataclass(frozen=True)
class FindingFacts:
    """What grouping needs to know about a stored finding."""

    detector: str
    finding_key: str
    agent_name: str | None
    agent_version: str | None
    tool_name: str | None
    error_type: str | None

    @property
    def subject(self) -> str:
        if self.tool_name:
            return self.tool_name
        return self.finding_key


@dataclass(frozen=True)
class Fingerprint:
    value: str
    components: Mapping[str, Any]


def validate_overrides(overrides: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for detector, components in (overrides or {}).items():
        if detector not in DEFAULT_COMPONENTS:
            raise ValueError(f"unknown detector {detector!r}")
        if not isinstance(components, Sequence) or isinstance(components, str):
            raise ValueError(f"{detector}: components must be a list")
        unknown = set(components) - set(COMPONENTS)
        if unknown:
            raise ValueError(f"{detector}: unknown components {sorted(unknown)}")
        if "detector" not in components:
            raise ValueError(f"{detector}: components must include 'detector'")
        result[detector] = tuple(components)
    return result


def fingerprint(facts: FindingFacts,
                overrides: Mapping[str, tuple[str, ...]] | None = None) -> Fingerprint:
    names = (overrides or {}).get(facts.detector) or DEFAULT_COMPONENTS[facts.detector]
    values: dict[str, Any] = {name: getattr(facts, name) for name in names}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:32]
    return Fingerprint(digest, values)


def title_for(components: Mapping[str, Any]) -> str:
    detector = components["detector"]
    tool = components.get("tool_name")
    subject = components.get("subject") or tool
    error = components.get("error_type")
    agent = components.get("agent_name")
    titles = {
        "unhandled_exception": f"Run fails with {error or 'an exception'}"
                               + (f" from '{tool}'" if tool else ""),
        "tool_error": f"'{tool or 'tool'}' fails" + (f" with {error}" if error else ""),
        "timeout": f"'{subject or 'call'}' times out",
        "repeated_tool_call": f"'{tool or 'tool'}' called repeatedly with identical arguments",
        "excessive_retries": f"'{subject or 'call'}' retried repeatedly after failing",
        "step_limit_reached": "Runs out of steps",
        "token_budget_exceeded": "Token budget exceeded"
                                 + (f" ({subject})" if subject else ""),
        "high_latency": f"Slow {subject}" if subject else "High latency",
    }
    title = titles.get(detector, detector)
    return f"{title} in {agent}" if agent else title


def description_for(detector: str) -> str:
    return _DESCRIPTIONS.get(detector, detector)
