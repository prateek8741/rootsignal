"""Server configuration, read from ``ROOTSIGNAL_*`` environment variables or ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
