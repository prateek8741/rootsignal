"""Detector thresholds. Defaults here; per-project overrides in ``projects.detector_config``.

Override shape: ``{"<detector>": {"enabled": false, "<param>": <value>}}``.
Unknown detectors or parameters are rejected so a typo can't silently
disable a check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any


@dataclass(frozen=True)
class RepeatedToolCallConfig:
    enabled: bool = True
    min_calls: int = 3  # identical (tool, arguments) calls in one trace


@dataclass(frozen=True)
class ExcessiveRetriesConfig:
    enabled: bool = True
    min_retries: int = 2  # identical attempts after the first failure


@dataclass(frozen=True)
class TokenBudgetConfig:
    enabled: bool = True
    max_tokens_per_trace: int = 100_000
    max_tokens_per_call: int = 32_000


@dataclass(frozen=True)
class HighLatencyConfig:
    enabled: bool = True
    max_trace_ms: float = 60_000
    max_tool_ms: float = 30_000
    max_llm_ms: float = 60_000


@dataclass(frozen=True)
class SimpleConfig:
    enabled: bool = True


@dataclass(frozen=True)
class DetectorConfig:
    unhandled_exception: SimpleConfig = field(default_factory=SimpleConfig)
    tool_error: SimpleConfig = field(default_factory=SimpleConfig)
    timeout: SimpleConfig = field(default_factory=SimpleConfig)
    repeated_tool_call: RepeatedToolCallConfig = field(default_factory=RepeatedToolCallConfig)
    excessive_retries: ExcessiveRetriesConfig = field(default_factory=ExcessiveRetriesConfig)
    step_limit_reached: SimpleConfig = field(default_factory=SimpleConfig)
    token_budget_exceeded: TokenBudgetConfig = field(default_factory=TokenBudgetConfig)
    high_latency: HighLatencyConfig = field(default_factory=HighLatencyConfig)

    @classmethod
    def from_overrides(cls, overrides: Mapping[str, Any] | None) -> DetectorConfig:
        config = cls()
        for detector, params in (overrides or {}).items():
            if detector not in cls.__dataclass_fields__:
                raise ValueError(f"unknown detector {detector!r}")
            if not isinstance(params, Mapping):
                raise ValueError(f"{detector}: overrides must be an object")
            current = getattr(config, detector)
            allowed = {f.name: f.type for f in fields(current)}
            for name, value in params.items():
                if name not in allowed:
                    raise ValueError(f"{detector}: unknown parameter {name!r}")
                expected = type(getattr(current, name))
                if expected is float and isinstance(value, int) and not isinstance(value, bool):
                    value = float(value)
                if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                    raise ValueError(f"{detector}.{name} must be {expected.__name__}")
                if expected in (int, float) and value <= 0:
                    raise ValueError(f"{detector}.{name} must be positive")
            config = replace(config, **{detector: replace(current, **params)})
        return config
