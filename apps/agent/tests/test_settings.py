"""Tests for `archmentor_agent.config.Settings`.

The `_isolated_env` fixture disables the developer's `.env` file and
seeds the two required credential fields, so each test sees a clean
process env and can assert against source defaults deterministically.
"""

from __future__ import annotations

import pytest
from archmentor_agent.brain.pricing import BRAIN_MODEL
from archmentor_agent.config import Settings, get_settings, reset_settings_cache
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsConfigDict

_TEST_AGENT_TOKEN = "test_agent_token_test_agent_token_test_agent_token"  # noqa: S105
_TEST_ANTHROPIC_KEY = "sk-ant-test-fixture-not-a-real-key"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test against a clean settings env.

    - Drops the lru_cache so a previous test's `Settings` instance
      doesn't leak into this one.
    - Replaces `Settings.model_config` with a copy that disables `.env`
      reads (otherwise the developer's local `.env` overrides would
      defeat `monkeypatch.delenv` and source-default assertions).
      `monkeypatch.setattr` restores the original after the test.
    - Seeds the two required credential env vars to non-placeholder
      values so most tests don't have to.
    """
    reset_settings_cache()
    config_no_env_file = SettingsConfigDict(**{**Settings.model_config, "env_file": None})
    monkeypatch.setattr(Settings, "model_config", config_no_env_file)
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", _TEST_AGENT_TOKEN)
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", _TEST_ANTHROPIC_KEY)


def test_source_defaults_resolve_when_env_file_disabled() -> None:
    """Asserts the in-source defaults (independent of any `.env` file)."""
    settings = get_settings()
    assert settings.api_url == "http://localhost:8000"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.whisper_model == "large-v3"
    assert settings.whisper_dir == ".model-cache/whisper"
    assert settings.tts_voice == "af_bella"
    assert settings.tts_device is None
    assert settings.env == "dev"
    assert settings.brain_enabled is True
    assert settings.hinglish_fallback is True
    assert settings.anthropic_base_url is None
    assert settings.brain_model == BRAIN_MODEL


def test_credentials_are_wrapped_in_secret_str() -> None:
    settings = get_settings()
    assert isinstance(settings.agent_ingest_token, SecretStr)
    assert isinstance(settings.anthropic_api_key, SecretStr)
    assert settings.agent_ingest_token.get_secret_value() == _TEST_AGENT_TOKEN
    assert settings.anthropic_api_key.get_secret_value() == _TEST_ANTHROPIC_KEY


def test_get_settings_caches_instance() -> None:
    assert get_settings() is get_settings()


def test_secret_str_does_not_leak_in_repr() -> None:
    """SecretStr renders as `SecretStr('**********')`; the raw values
    must not appear anywhere in repr/str of `Settings`."""
    settings = get_settings()
    rendered = repr(settings)
    assert _TEST_AGENT_TOKEN not in rendered
    assert _TEST_ANTHROPIC_KEY not in rendered


def test_missing_agent_ingest_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHMENTOR_AGENT_INGEST_TOKEN", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings()  # ty: ignore[missing-argument]
    assert "agent_ingest_token" in str(exc.value).lower()


def test_missing_anthropic_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHMENTOR_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings()  # ty: ignore[missing-argument]
    assert "anthropic_api_key" in str(exc.value).lower()


def test_placeholder_value_rejected_for_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "replace_with_anthropic_key")
    with pytest.raises(ValidationError) as exc:
        Settings()  # ty: ignore[missing-argument]
    assert "placeholder" in str(exc.value).lower()


def test_placeholder_value_rejected_for_agent_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "replace_with_token_value")
    with pytest.raises(ValidationError) as exc:
        Settings()  # ty: ignore[missing-argument]
    assert "placeholder" in str(exc.value).lower()


def test_brain_enabled_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHMENTOR_BRAIN_ENABLED", "false")
    assert Settings().brain_enabled is False  # ty: ignore[missing-argument]


def test_brain_enabled_accepts_truthy_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHMENTOR_BRAIN_ENABLED", "1")
    assert Settings().brain_enabled is True  # ty: ignore[missing-argument]


def test_overrides_take_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_WHISPER_MODEL", "tiny.en")
    monkeypatch.setenv("ARCHMENTOR_TTS_VOICE", "bf_emma")
    settings = Settings()  # ty: ignore[missing-argument]
    assert settings.api_url == "http://api.test:9999"
    assert settings.whisper_model == "tiny.en"
    assert settings.tts_voice == "bf_emma"


def test_unknown_env_vars_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`extra="ignore"` so LIVEKIT_*, HOME, etc. don't trip Settings."""
    monkeypatch.setenv("LIVEKIT_URL", "ws://localhost:7880")
    monkeypatch.setenv("ARCHMENTOR_NOT_A_REAL_FIELD", "garbage")
    Settings()  # ty: ignore[missing-argument]  # constructs without error


def test_anthropic_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_BASE_URL", "https://api.getunbound.ai")
    assert Settings().anthropic_base_url == "https://api.getunbound.ai"  # ty: ignore[missing-argument]


def test_brain_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHMENTOR_BRAIN_MODEL", "claude-opus-4-7")
    assert Settings().brain_model == "claude-opus-4-7"  # ty: ignore[missing-argument]
