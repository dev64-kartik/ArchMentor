"""Entry-point smoke tests.

The full entrypoint needs LiveKit room state + framework adapters, so
we can't run it end-to-end here. We test the pure helpers that gate
the agent's setup: session id parsing and ledger-config env reads.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from archmentor_agent.config import reset_settings_cache
from archmentor_agent.main import _ledger_config, _session_id_from_ctx
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """`Settings` is process-wide cached; clear it so monkeypatched env
    vars take effect on each test."""
    reset_settings_cache()


class _FakeRoom:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCtx:
    def __init__(self, room_name: str) -> None:
        self.room = _FakeRoom(room_name)


def test_session_id_from_session_prefixed_room() -> None:
    sid = UUID("12345678-1234-5678-1234-567812345678")
    ctx = _FakeCtx(f"session-{sid}")
    assert _session_id_from_ctx(ctx) == sid  # ty: ignore[invalid-argument-type]


def test_session_id_from_bare_uuid_room() -> None:
    sid = UUID("12345678-1234-5678-1234-567812345678")
    ctx = _FakeCtx(str(sid))
    assert _session_id_from_ctx(ctx) == sid  # ty: ignore[invalid-argument-type]


def test_session_id_raises_on_garbage_room() -> None:
    ctx = _FakeCtx("my-test-room")
    with pytest.raises(RuntimeError, match="Cannot extract session UUID"):
        _session_id_from_ctx(ctx)  # ty: ignore[invalid-argument-type]


def _disable_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `Settings` from reading the developer's `.env` file.

    `_ledger_config` constructs `Settings()` internally, so monkeypatching
    `os.environ` alone isn't enough — pydantic-settings reads `.env` too.
    Replace `model_config` with a copy that has `env_file=None` for the
    duration of the test; `monkeypatch.setattr` restores the original.
    """
    from archmentor_agent.config import Settings
    from pydantic_settings import SettingsConfigDict

    config_no_env_file = SettingsConfigDict(**{**Settings.model_config, "env_file": None})
    monkeypatch.setattr(Settings, "model_config", config_no_env_file)


def test_ledger_config_requires_agent_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings construction raises ValidationError when the token is
    absent. The raw RuntimeError from M1's `_ledger_config` was replaced
    by pydantic-settings' own validation."""
    _disable_env_file(monkeypatch)
    monkeypatch.delenv("ARCHMENTOR_AGENT_INGEST_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="agent_ingest_token"):
        _ledger_config()


def test_ledger_config_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_env_file(monkeypatch)
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok_test_tok_test_tok_test_tok")
    cfg = _ledger_config()
    assert cfg.base_url == "http://api.test:9999"
    assert cfg.agent_token == "tok_test_tok_test_tok_test_tok"  # noqa: S105 — fixture value
