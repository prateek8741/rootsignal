"""Server configuration, read from ``ROOTSIGNAL_*`` environment variables or ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROOTSIGNAL_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost:5432/rootsignal_dev"
    database_pool_size: int = Field(default=5, ge=1)
    database_echo: bool = False

    # Ingest request limits (per request, before and after decompression).
    max_request_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    max_decompressed_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    # Per-API-key token bucket, per server process.
    rate_limit_per_second: float = Field(default=50.0, gt=0)
    rate_limit_burst: int = Field(default=100, ge=1)
    # Optional JSON price table; see docs/COST_MODEL.md. Without it only
    # explicitly zero-cost providers (local models) are priced.
    pricing_file: Path | None = None
    # Bound on the JSONB attributes stored per span.
    max_attribute_value_chars: int = Field(default=4096, ge=256)
    max_span_attributes_bytes: int = Field(default=65_536, ge=4096)

    # Dashboard sessions (see docs/THREAT_MODEL.md §3.3).
    session_absolute_ttl_hours: float = Field(default=12, gt=0)
    session_idle_minutes: float = Field(default=120, gt=0)
    # Must be true whenever the dashboard is served over HTTPS (any non-local deploy).
    session_cookie_secure: bool = False
    login_link_ttl_minutes: float = Field(default=5, gt=0, le=60)
    # Unauthenticated login endpoints, per client IP, per server process.
    login_rate_limit_per_minute: int = Field(default=30, ge=1)

    # Platform LLM for investigations (see docs/INVESTIGATIONS.md).
    llm_provider: Literal["none", "ollama", "openai_compatible"] = "none"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "llama3.1:8b"
    llm_api_key: SecretStr | None = None
    # Refuse non-loopback providers unless explicitly allowed: evidence is customer data.
    llm_allow_external: bool = False
    llm_timeout_s: float = Field(default=120, gt=0)
    llm_max_output_tokens: int = Field(default=800, ge=64, le=8192)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
