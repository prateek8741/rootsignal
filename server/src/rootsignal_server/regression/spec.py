"""The regression case spec: a self-contained, versioned JSON document.

It is what the replay runner (increment 8) executes and what teams can check
into their repository for CI. ``schema_version`` changes only with breaking
changes to this format.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

AssertionKind = Literal[
    "detector_absent", "max_identical_tool_calls", "max_llm_calls", "no_unhandled_exception",
    "run_completed", "max_total_tokens", "max_duration_ms",
]
# What an assertion needs in order to be able to pass after a correct fix:
# recorded = tool results and model outputs replayed from fixtures;
# live_tools / live_model / live = those parts must run for real.
Requirement = Literal["recorded", "live_tools", "live_model", "live"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assertion(_Strict):
    id: str
    kind: AssertionKind
    params: dict[str, Any] = Field(default_factory=dict)
    description: str
    # reproduces: encodes the failure, so it must FAIL on the source run.
    # guard: protects against a "fix" that breaks something else; must PASS on it.
    role: Literal["reproduces", "guard"]
    requires: Requirement
    derived_from: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class Payload(_Strict):
    """A recorded value plus what we know about its fidelity."""

    captured: bool
    value: Any = None
    truncated: bool = False
    redacted: bool = False


class ToolFixture(_Strict):
    call_index: int
    tool: str
    side_effect: str
    args_hash: str | None
    repeat_index: int | None
    arguments: Payload
    outcome: Literal["result", "error"]
    result: Payload | None = None
    error_type: str | None = None
    error_message: str | None = None


class LLMFixture(_Strict):
    call_index: int
    model: str | None
    provider: str | None
    output: Payload
    input_tokens: int | None
    output_tokens: int | None
    failed: bool = False
    error_type: str | None = None


class RetrievalFixture(_Strict):
    position: int
    data_source: str | None
    query: Payload
    documents: Payload


class Source(_Strict):
    trace_id: str
    issue_id: str
    agent_name: str | None
    agent_version: str | None
    started_at: str
    completion_reason: str | None


class Fixtures(_Strict):
    tools: list[ToolFixture] = Field(default_factory=list)
    llm: list[LLMFixture] = Field(default_factory=list)
    retrievals: list[RetrievalFixture] = Field(default_factory=list)


class Quality(_Strict):
    """Whether recorded replay is possible, and every known gap in fidelity."""

    replayable_recorded: bool
    warnings: list[str] = Field(default_factory=list)
    dropped_assertions: list[dict[str, str]] = Field(default_factory=list)


class CaseInput(_Strict):
    # kwargs: call the entrypoint with these keyword arguments (decorated agents);
    # value: a single positional argument; missing: not captured and not supplied.
    kind: Literal["kwargs", "value", "missing"]
    value: Any = None
    supplied_by_user: bool = False
    redacted: bool = False
    truncated: bool = False


class CaseSpec(_Strict):
    schema_version: Literal[1] = 1
    name: str
    source: Source
    entrypoint: str | None
    input: CaseInput
    fixtures: Fixtures
    assertions: list[Assertion]
    quality: Quality
