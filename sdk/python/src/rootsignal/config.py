"""SDK configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

from rootsignal.redaction import Redactor


class ConfigError(ValueError):
    """Raised when SDK configuration is invalid. Only raised from ``init``."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the SDK.

    The API key identifies the project and environment on the server, so neither
    is configured here. ``environment`` is sent as a resource attribute for
    display only; the server trusts the key's environment.
    """

    api_key: str | None = None
    endpoint: str = "http://127.0.0.1:8000"
    service_name: str = "agent"
    environment: str = "development"
    enabled: bool = True
    sample_rate: float = 1.0
    # Model, tool and retrieval inputs/outputs.
    capture_payloads: bool = True
    # The end user's request: agent-run input and user-role model messages.
    capture_user_input: bool = False
    redact_pii: bool = True
    redactor: Redactor | None = None
    # Optional key for HMAC-ing tool arguments; see TRACE_SCHEMA.md.
    args_hash_key: str | None = None
    max_payload_bytes: int = 32_768
    export_timeout_s: float = 5.0
    max_queue_size: int = 2048
    max_export_batch_size: int = 256
    schedule_delay_ms: int = 1000
    extra_resource_attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ConfigError(f"sample_rate must be within [0, 1], got {self.sample_rate}")
        if self.max_payload_bytes < 256:
            raise ConfigError("max_payload_bytes must be at least 256")
        if self.export_timeout_s <= 0:
            raise ConfigError("export_timeout_s must be positive")
        if self.max_export_batch_size > self.max_queue_size:
            raise ConfigError("max_export_batch_size cannot exceed max_queue_size")
        if not self.endpoint.startswith(("http://", "https://")):
            raise ConfigError(f"endpoint must be an http(s) URL, got {self.endpoint!r}")

    @property
    def traces_url(self) -> str:
        return self.endpoint.rstrip("/") + "/v1/traces"

    def effective_redactor(self) -> Redactor | None:
        if not self.redact_pii:
            return None
        return self.redactor or Redactor()

    @classmethod
    def from_env(cls, **overrides: Any) -> Config:
        """Build a config from ``ROOTSIGNAL_*`` variables; keyword overrides win."""
        defaults = cls()
        base = cls(
            api_key=os.environ.get("ROOTSIGNAL_API_KEY") or None,
            endpoint=os.environ.get("ROOTSIGNAL_ENDPOINT", defaults.endpoint),
            service_name=os.environ.get("ROOTSIGNAL_SERVICE_NAME", defaults.service_name),
            environment=os.environ.get("ROOTSIGNAL_ENVIRONMENT", defaults.environment),
            enabled=_env_bool("ROOTSIGNAL_ENABLED", defaults.enabled),
            sample_rate=_env_float("ROOTSIGNAL_SAMPLE_RATE", defaults.sample_rate),
            capture_payloads=_env_bool("ROOTSIGNAL_CAPTURE_PAYLOADS", defaults.capture_payloads),
            capture_user_input=_env_bool(
                "ROOTSIGNAL_CAPTURE_USER_INPUT", defaults.capture_user_input
            ),
            redact_pii=_env_bool("ROOTSIGNAL_REDACT_PII", defaults.redact_pii),
            args_hash_key=os.environ.get("ROOTSIGNAL_ARGS_HASH_KEY") or None,
        )
        return replace(base, **overrides) if overrides else base
