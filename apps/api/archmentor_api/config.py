"""Runtime configuration.

Env vars are prefixed `API_` and loaded from `.env` in dev.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> archmentor_api -> apps/api -> apps -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Process-wide settings resolved once at startup."""

    model_config = SettingsConfigDict(
        # Anchor `.env` at the repo root so alembic (run from apps/api/)
        # picks up the same file the FastAPI process does.
        env_file=str(_REPO_ROOT / ".env"),
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
        description="Must match GOTRUE_JWT_SECRET. Required — no default.",
        min_length=32,
    )
    jwt_audience: str = "authenticated"
    jwt_issuer: str = Field(
        default="http://localhost:9999",
        description="GoTrue `iss` claim. Must match GOTRUE_API_EXTERNAL_URL.",
    )

    cors_origins: list[str] = ["http://localhost:3000"]

    # LiveKit (token minting for the browser client).
    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = Field(
        description="LiveKit API key. Required — no default.",
        min_length=1,
    )
    livekit_api_secret: str = Field(
        description="LiveKit API secret. Required — no default.",
        min_length=32,
    )
    livekit_token_ttl_s: int = 900  # 15 minutes

    # Shared secret the agent worker presents when appending to the event
    # ledger. Not a user JWT — the agent is a trusted backend peer.
    agent_ingest_token: str = Field(
        description="Shared secret for agent→API event ingest. Required — no default.",
        min_length=32,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance.

    `Settings()` reads `jwt_secret` from `API_JWT_SECRET`; ty cannot see
    through pydantic-settings' env-var resolution, so the "required kwarg"
    diagnostic is a false positive here.
    """
    return Settings()  # ty: ignore[missing-argument]
