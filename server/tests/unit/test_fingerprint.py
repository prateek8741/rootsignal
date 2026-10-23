from __future__ import annotations

from typing import Any

import pytest

from rootsignal_server.detection.detectors import DETECTOR_NAMES
from rootsignal_server.issues.fingerprint import (
    DEFAULT_COMPONENTS,
    FindingFacts,
    description_for,
    fingerprint,
    title_for,
    validate_overrides,
)


def facts(detector: str = "tool_error", key: str = "get_account:Boom", agent: str = "support",
          version: str = "v1", tool: str | None = "get_account",
          error: str | None = "Boom") -> FindingFacts:
    return FindingFacts(detector, key, agent, version, tool, error)


def test_every_detector_has_default_components_and_text() -> None:
    assert set(DEFAULT_COMPONENTS) == set(DETECTOR_NAMES)
    for name in DETECTOR_NAMES:
        assert "detector" in DEFAULT_COMPONENTS[name]
        assert description_for(name) != name


def test_same_problem_different_arguments_share_a_fingerprint() -> None:
    a = fingerprint(facts("repeated_tool_call", key="get_account:aaaa", error=None))
    b = fingerprint(facts("repeated_tool_call", key="get_account:bbbb", error="Boom"))
    assert a.value == b.value  # args hash and error type are not components here
    other_tool = fingerprint(facts("repeated_tool_call", key="x:aaaa", tool="search"))
    assert other_tool.value != a.value


def test_step_limit_ignores_the_per_trace_span_key() -> None:
    a = fingerprint(facts("step_limit_reached", key="1111111111111111", tool=None))
    b = fingerprint(facts("step_limit_reached", key="2222222222222222", tool=None))
    assert a.value == b.value


def test_version_does_not_split_issues_by_default() -> None:
    assert fingerprint(facts(version="v1")).value == fingerprint(facts(version="v2")).value


def test_subject_names_tools_or_models() -> None:
    tool_timeout = fingerprint(facts("timeout", key="tool:get_account"))
    llm_timeout = fingerprint(facts("timeout", key="llm:llama3", tool=None))
    assert tool_timeout.components["subject"] == "get_account"
    assert llm_timeout.components["subject"] == "llm:llama3"
    assert tool_timeout.value != llm_timeout.value


def test_detectors_never_collide() -> None:
    values = {fingerprint(facts(d)).value for d in DETECTOR_NAMES}
    assert len(values) == len(DETECTOR_NAMES)


def test_overrides() -> None:
    merge_errors = validate_overrides({"tool_error": ["detector", "agent_name", "tool_name"]})
    assert fingerprint(facts(error="A"), merge_errors).value == \
        fingerprint(facts(error="B"), merge_errors).value
    assert fingerprint(facts(error="A")).value != fingerprint(facts(error="B")).value
    by_version = validate_overrides({"tool_error": ["detector", "tool_name", "agent_version"]})
    assert fingerprint(facts(version="v1"), by_version).value != \
        fingerprint(facts(version="v2"), by_version).value


@pytest.mark.parametrize("overrides", [
    {"nope": ["detector"]},
    {"tool_error": ["agent_name", "tool_name"]},  # must include detector
    {"tool_error": ["detector", "args_hash"]},
    {"tool_error": "detector"},
])
def test_invalid_overrides(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_overrides(overrides)


def test_titles_are_built_from_components() -> None:
    for detector in DETECTOR_NAMES:
        title = title_for(fingerprint(facts(detector)).components)
        assert title.endswith("in support")
    assert title_for(fingerprint(facts()).components) == "'get_account' fails with Boom in support"
