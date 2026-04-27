"""Agent worker runtime configuration.

Env vars are prefixed `ARCHMENTOR_` and loaded from `.env` at the repo
root in dev (the livekit-agents CLI does not load `.env` itself, so the
agent's `main()` calls `load_dotenv()` early — see `main.py`).

This module is the single source of truth for agent config: the ledger
client, brain client, Redis store, STT, and TTS all read through
`get_settings()`. Direct `os.environ.get(...)` reads are intentionally
removed so a forgotten env var fails loud at startup, not at first use.

Credential fields use `SecretStr` so a stray `repr(settings)` in a
structlog context or exception traceback can't leak the raw value.
Always call `.get_secret_value()` at the boundary (HTTP header, SDK
client kwarg) — never log the wrapped object.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default model id is duplicated from `brain.pricing.BRAIN_MODEL` rather
# than imported: `brain/__init__.py` transitively pulls `state` →
# `redis_store` → `config`, which re-enters this module mid-load and
# triggers a circular import. `test_settings.py::test_source_defaults…`
# asserts `Settings().brain_model == brain.pricing.BRAIN_MODEL` so drift
# between the two strings is caught by CI.
_DEFAULT_BRAIN_MODEL = "anthropic/claude-opus-4-7"

# config.py -> archmentor_agent -> apps/agent -> apps -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Mirrors `archmentor_api.config._PLACEHOLDER_MARKER`. The `.env.example`
# placeholders contain `replace_with_` so a forgotten copy fails loud
# rather than booting with publicly-known credentials.
_PLACEHOLDER_MARKER = "replace_with_"


class Settings(BaseSettings):
    """Agent process-wide settings resolved once at startup."""

    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_prefix="ARCHMENTOR_",
        env_file_encoding="utf-8",
        # The framework reads LIVEKIT_URL / LIVEKIT_API_KEY / etc. directly
        # from os.environ. Tolerate those (and the developer's wider
        # shell environment) without complaining.
        extra="ignore",
    )

    # ─── Environment ────────────────────────────────────────────────────
    env: str = Field(
        default="dev",
        description="`dev` enables `.env` overrides in main(); any other "
        "value runs shell-wins so the orchestrator can't be silently "
        "overridden by a stale on-disk .env.",
    )

    # ─── Brain control ──────────────────────────────────────────────────
    brain_enabled: bool = Field(
        default=True,
        description="Kill switch. When False, MentorAgent falls back to "
        "the M1 static-ack path so STT/TTS iteration isn't blocked by a "
        "broken Anthropic key or quota.",
    )

    # ─── Control-plane API ──────────────────────────────────────────────
    api_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for the FastAPI control plane (event ledger + snapshot ingest).",
    )
    agent_ingest_token: SecretStr = Field(
        description="Shared secret presented on POST /sessions/{id}/events "
        "and /snapshots. Must equal API_AGENT_INGEST_TOKEN. Required — "
        "no default.",
    )

    # ─── Anthropic / Anthropic-compatible gateway ───────────────────────
    anthropic_api_key: SecretStr = Field(
        description="API key for the brain client. Accepts a direct "
        "Anthropic key or a gateway key (Unbound, LiteLLM, etc. that "
        "expose the Anthropic Messages API). Required — no default. "
        "Wrapped in SecretStr so a stray repr/log call can't leak it.",
    )
    anthropic_base_url: str | None = Field(
        default=None,
        description="Override the Anthropic SDK's default base URL. Set "
        "to e.g. `https://api.getunbound.ai` to route through Unbound. "
        "When None, the SDK talks to api.anthropic.com directly.",
    )
    brain_model: str = Field(
        default=_DEFAULT_BRAIN_MODEL,
        description="Model id passed to `messages.create(model=...)`. "
        "Default matches the Unbound provider-prefixed form; set to "
        "`claude-opus-4-7` for direct Anthropic. Must be a key in "
        "`brain/pricing.py::BRAIN_RATES` or cost estimation raises.",
    )
    brain_haiku_model: str = Field(
        default="anthropic/claude-haiku-4-5",
        description="Model id for the per-session summary compactor "
        "(M4 Unit 5). Default is the Unbound provider-prefixed form; "
        "set to `claude-haiku-4-5` for direct Anthropic. Must be a key "
        "in `brain/pricing.py::BRAIN_RATES`. The compaction-trigger "
        "threshold is intentionally NOT a Settings field — it lives as "
        "the inline `_SUMMARY_COMPACTION_THRESHOLD` constant in "
        "`brain/haiku_client.py` next to the prompt builder.",
    )

    # ─── Redis ──────────────────────────────────────────────────────────
    # Plain str (not SecretStr): redis.asyncio.from_url consumes the URL
    # as-is and would not accept `.get_secret_value()` without unwrapping
    # at every call site. Local-dev URL has no embedded credentials.
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="redis-py URL for SessionState persistence.",
    )

    # ─── Audio (Apple Silicon only; lazy-loaded) ────────────────────────
    whisper_model: str = Field(
        default="large-v3",
        description="whisper.cpp model id. `large-v3` is the M1-tested "
        "default and what `scripts/warm_models.py` prewarms.",
    )
    whisper_dir: str = Field(
        default=".model-cache/whisper",
        description="Directory whisper.cpp downloads + caches model "
        "weights into. Repo-local because the Claude sandbox denies "
        "writes to pywhispercpp's user-data default.",
    )
    tts_voice: str = Field(
        default="af_bella",
        description="Kokoro voice id.",
    )
    tts_device: str | None = Field(
        default=None,
        description="Kokoro device override (e.g. `mps`). When None, "
        "streaming-tts picks per its own default.",
    )
    tts_speed: float = Field(
        default=0.9,
        description="Kokoro `default_speed` — 1.0 is native cadence; "
        "0.9 is ~10% slower, which listeners report as calmer and more "
        "interviewer-like. Lower bound is streaming-tts's own floor; "
        "values below ~0.7 distort vowel formants.",
    )
    hinglish_fallback: bool = Field(
        default=True,
        description="When True (M2 default), short whisper buffers "
        "(<3s) auto-detected as neither English nor Hindi are re-run "
        "with `language='en'` to dodge whisper.cpp's tendency to label "
        "short Indian-accented English as Welsh/Irish/Nynorsk.",
    )

    @field_validator("agent_ingest_token", "anthropic_api_key")
    @classmethod
    def _reject_placeholder(cls, value: SecretStr) -> SecretStr:
        """Refuse any value that still contains the `.env.example` marker.

        Mirrors `archmentor_api.config.Settings._reject_env_example_placeholder`.
        Operates on the unwrapped string but returns the wrapped SecretStr
        so the wrapping is preserved end-to-end.
        """
        if _PLACEHOLDER_MARKER in value.get_secret_value():
            raise ValueError(
                "Value still contains the `.env.example` placeholder — "
                "replace it with a real secret before starting the agent."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance.

    `Settings()` reads required fields from env; ty cannot see through
    pydantic-settings' env-var resolution, so the `missing-argument`
    diagnostic is a false positive.
    """
    return Settings()  # ty: ignore[missing-argument]


def reset_settings_cache() -> None:
    """Clear the `get_settings()` cache. Test-only helper.

    Production code should never call this — `Settings` is meant to be
    immutable for the lifetime of the process. Tests use it to apply
    env-var changes between cases.
    """
    get_settings.cache_clear()
