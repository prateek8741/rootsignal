"""Server configuration, read from ``ROOTSIGNAL_*`` environment variables or ``.env``."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROOTSIGNAL_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost:5432/rootsignal_dev"
    database_pool_size: int = Field(default=5, ge=1)
    database_echo: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
