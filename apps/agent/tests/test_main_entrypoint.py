"""Entry-point smoke tests.

The full entrypoint needs LiveKit room state + framework adapters, so
we can't run it end-to-end here. We test the pure helpers that gate
the agent's setup: session id parsing and ledger-config env reads.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from archmentor_agent.main import _ledger_config, _session_id_from_ctx


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


def test_ledger_config_requires_agent_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHMENTOR_AGENT_INGEST_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ARCHMENTOR_AGENT_INGEST_TOKEN"):
        _ledger_config()


def test_ledger_config_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok")
    cfg = _ledger_config()
    assert cfg.base_url == "http://api.test:9999"
    assert cfg.agent_token == "tok"  # noqa: S105 — fixture value
