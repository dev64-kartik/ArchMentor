"""Runtime configuration.

Env vars are prefixed `API_` and loaded from `.env` in dev.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings resolved once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="API_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"  # noqa: S104 — bind to all interfaces in container
    port: int = 8000
    debug: bool = False

    database_url: str = "postgresql+psycopg://postgres:postgres_dev@localhost:5432/archmentor"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = Field(
        default="dev_jwt_secret_change_me_minimum_32_chars",
        description="Must match GOTRUE_JWT_SECRET for local verification.",
    )
    jwt_audience: str = "authenticated"
    jwt_issuer: str | None = None

    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
