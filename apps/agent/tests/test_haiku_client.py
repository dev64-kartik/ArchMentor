"""Tests for `archmentor_agent.brain.haiku_client.HaikuClient` (M4 Unit 5).

Mirrors `test_brain_client.py`'s pattern — the SDK transport is replaced
by a `_FakeMessages` shim so we don't hit the network. The compactor's
contract is small: a single ``messages.create`` call, a string-typed
result, a `BrainUsage` populated from ``response.usage``, and 800-char
client-side truncation.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, Usage
from archmentor_agent.brain.haiku_client import (
    _SUMMARY_COMPACTION_THRESHOLD,
    HaikuClient,
    get_haiku_client,
    reset_haiku_client_singleton,
)
from archmentor_agent.brain.haiku_prompt import SUMMARY_MAX_CHARS, SYSTEM_PROMPT
from archmentor_agent.brain.pricing import HAIKU_4_5_RATES
from archmentor_agent.config import Settings, reset_settings_cache
from archmentor_agent.state.session_state import TranscriptTurn

# `pytest-asyncio` runs in auto mode (see root pyproject.toml's
# `[tool.pytest.ini_options]`), so async test functions are detected
# automatically — no module-level pytestmark needed and the sync
# constant test below stays clean of the marker.


# ──────────────────────────────────────────────────────────────────────
# Fixtures + fakes
# ──────────────────────────────────────────────────────────────────────


def _make_message(*, text: str, input_tokens: int = 200, output_tokens: int = 60) -> Message:
    return Message(
        id="msg_haiku",
        content=[TextBlock(type="text", text=text)],
        model="anthropic/claude-haiku-4-5",
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _FakeMessages:
    def __init__(self, responder):  # type: ignore[no-untyped-def]
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        result = self._responder(kwargs)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeAnthropic:
    def __init__(self, responder):  # type: ignore[no-untyped-def]
        self.messages = _FakeMessages(responder)

    async def close(self) -> None:
        return None


def _client(responder) -> HaikuClient:  # type: ignore[no-untyped-def]
    # `settings` is unused when `client` is provided.
    return HaikuClient(
        settings=cast(Settings, None),
        client=cast(AsyncAnthropic, _FakeAnthropic(responder)),
        model="anthropic/claude-haiku-4-5",
    )


def _turns(*texts: str) -> list[TranscriptTurn]:
    return [
        TranscriptTurn(t_ms=i * 1_000, speaker="candidate", text=text)
        for i, text in enumerate(texts)
    ]


# ──────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────


async def test_compress_returns_summary_text_and_usage() -> None:
    summary = "Candidate proposed sharded MySQL with consistent hashing for short-code lookup."
    captured: dict[str, Any] = {}

    def responder(kwargs: dict[str, Any]) -> Message:
        captured.update(kwargs)
        return _make_message(text=summary)

    client = _client(responder)
    new_summary, usage = await client.compress(
        existing_summary="Earlier: scoped functional requirements.",
        dropped_turns=_turns("how do you shard?", "consistent hashing", "ok let's go"),
    )

    assert new_summary == summary
    # Token + cost roll-up uses the Haiku rate card.
    assert usage.input_tokens == 200
    assert usage.output_tokens == 60
    expected_cost = 200 * HAIKU_4_5_RATES.input_per_token + 60 * HAIKU_4_5_RATES.output_per_token
    assert usage.cost_usd == pytest.approx(expected_cost)

    # Single call, correct system prompt + model, user message includes
    # both the existing summary and the dropped turns.
    assert captured["model"] == "anthropic/claude-haiku-4-5"
    assert captured["system"] == SYSTEM_PROMPT
    assert captured["max_tokens"] == 512
    user_msg = captured["messages"][0]["content"]
    assert "Earlier: scoped functional requirements." in user_msg
    assert "candidate: how do you shard?" in user_msg
    assert "candidate: consistent hashing" in user_msg


async def test_compress_with_empty_existing_summary_renders_placeholder() -> None:
    captured: dict[str, Any] = {}

    def responder(kwargs: dict[str, Any]) -> Message:
        captured.update(kwargs)
        return _make_message(text="ok")

    client = _client(responder)
    await client.compress(existing_summary="", dropped_turns=_turns("hi"))

    user_msg = captured["messages"][0]["content"]
    # `(none yet)` is the marker the prompt builder injects so the
    # model doesn't hallucinate a missing-summary block.
    assert "(none yet)" in user_msg


# ──────────────────────────────────────────────────────────────────────
# Truncation
# ──────────────────────────────────────────────────────────────────────


async def test_long_summary_is_truncated_to_max_chars() -> None:
    long_text = "x" * (SUMMARY_MAX_CHARS + 100)

    def responder(_kwargs: dict[str, Any]) -> Message:
        return _make_message(text=long_text)

    client = _client(responder)
    summary, _ = await client.compress(existing_summary="", dropped_turns=_turns("a"))
    assert len(summary) == SUMMARY_MAX_CHARS
    assert summary.endswith("…")


async def test_summary_at_max_chars_is_unchanged() -> None:
    exact_text = "x" * SUMMARY_MAX_CHARS

    def responder(_kwargs: dict[str, Any]) -> Message:
        return _make_message(text=exact_text)

    client = _client(responder)
    summary, _ = await client.compress(existing_summary="", dropped_turns=_turns("a"))
    assert summary == exact_text


# ──────────────────────────────────────────────────────────────────────
# Error propagation
# ──────────────────────────────────────────────────────────────────────


async def test_haiku_call_error_propagates_to_caller() -> None:
    """`HaikuClient.compress` does NOT degrade on errors — the agent's
    `_run_compaction` catches and logs `summary_compression_failed`.

    Letting the exception out keeps the contract symmetric with the
    brain client's discriminated stay_silent path: the compactor isn't
    in the voice loop, so a failure is a logged miss, not a per-turn
    fallback.
    """
    boom = RuntimeError("haiku 5xx after retries")

    def responder(_kwargs: dict[str, Any]) -> RuntimeError:
        return boom

    client = _client(responder)
    with pytest.raises(RuntimeError, match="haiku 5xx"):
        await client.compress(existing_summary="", dropped_turns=_turns("a"))


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────


async def test_get_haiku_client_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    reset_haiku_client_singleton()
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "test_token")
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "test_key")
    from archmentor_agent.config import get_settings as _get_settings

    a = get_haiku_client(_get_settings())
    b = get_haiku_client(_get_settings())
    assert a is b
    reset_haiku_client_singleton()


# ──────────────────────────────────────────────────────────────────────
# Module constant invariants
# ──────────────────────────────────────────────────────────────────────


def test_summary_compaction_threshold_is_inline_constant() -> None:
    # Refinements R6: the threshold lives next to `HaikuClient` rather
    # than as a Settings field. Pin the value so a future bump is
    # explicit and visible in the diff.
    assert _SUMMARY_COMPACTION_THRESHOLD == 30
