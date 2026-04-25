"""Tests for `archmentor_agent.brain.client.BrainClient`.

We don't hit the real Anthropic API — the `client` kwarg on
`BrainClient` is a test seam that takes any object exposing a
`messages.create(...)` awaitable. `_FakeAnthropic` below mimics the
SDK surface just enough to assert routing decisions on different
response shapes.

Scenarios covered (per M2 plan §Unit 3 test scenarios):

- Happy path tool_use → BrainDecision populated
- Cache usage summed into tokens_input
- stop_reason != "tool_use" → stay_silent("unexpected_stop_reason")
- Tool block missing required field → stay_silent("schema_violation")
- Confidence out of range → stay_silent("schema_violation")
- Long utterance → stay_silent("utterance_too_long")
- Control-char utterance → stay_silent("utterance_has_control_chars")
- AuthenticationError → raises (caller bug)
- BadRequestError → raises (caller bug)
- RateLimitError (after SDK retries) → stay_silent("api_error")
- APIConnectionError → stay_silent("api_error")
- CancelledError → propagates (router cancellation contract)
- tool_use.input not a dict → stay_silent("schema_violation")
- Missing tool block on stop_reason=tool_use → stay_silent
- Kwargs routed to messages.create match build_call_kwargs
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision
from archmentor_agent.brain.pricing import BRAIN_MODEL, estimate_cost_usd
from archmentor_agent.config import Settings
from archmentor_agent.state import SessionState
from archmentor_agent.state.session_state import (
    InterviewPhase,
    ProblemCard,
    TranscriptTurn,
)

# ────────────────────────── fixtures ──────────────────────────────────


def _state() -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="Design URL Shortener",
            statement_md="Design a URL shortener.",
            rubric_yaml="dimensions: [functional]",
        ),
        system_prompt_version="m2-initial",
        started_at=datetime(2026, 4, 22, 10, 0, tzinfo=UTC),
        phase=InterviewPhase.REQUIREMENTS,
        transcript_window=[
            TranscriptTurn(t_ms=1_000, speaker="candidate", text="Let me think."),
        ],
    )


def _valid_tool_input(**overrides: Any) -> dict[str, Any]:
    base = {
        "reasoning": "Candidate started with functional requirements; good opening.",
        "decision": "stay_silent",
        "priority": "low",
        "confidence": 0.7,
        "utterance": None,
    }
    base.update(overrides)
    return base


def _make_message(
    *,
    tool_input: dict[str, Any] | None,
    stop_reason: str = "tool_use",
    input_tokens: int = 500,
    output_tokens: int = 80,
    cache_creation_input_tokens: int | None = 0,
    cache_read_input_tokens: int | None = 0,
    include_text_block: bool = True,
    include_tool_block: bool = True,
) -> Message:
    content: list[Any] = []
    if include_text_block:
        content.append(TextBlock(type="text", text="thinking aloud"))
    if include_tool_block:
        content.append(
            ToolUseBlock(
                type="tool_use",
                id="toolu_abc",
                name="interview_decision",
                input=tool_input if tool_input is not None else {},
            )
        )
    return Message(
        id="msg_test",
        content=content,
        model=BRAIN_MODEL,
        role="assistant",
        stop_reason=cast(Any, stop_reason),
        stop_sequence=None,
        type="message",
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        ),
    )


class _FakeMessages:
    def __init__(self, responder):  # type: ignore[no-untyped-def]
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        result = self._responder(kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result


class _FakeAnthropic:
    """Satisfies the subset of AsyncAnthropic surface BrainClient uses.

    We only need `messages.create(...)` and `close()`. The real SDK has
    retry middleware in front of `messages.create`; we test post-retry
    behavior by having the fake raise the post-retry exception
    directly. This mirrors the ledger-client test approach where
    `httpx.MockTransport` stands in for the real transport.
    """

    def __init__(self, responder):  # type: ignore[no-untyped-def]
        self.messages = _FakeMessages(responder)

    async def close(self) -> None:
        return None


def _client(responder) -> BrainClient:  # type: ignore[no-untyped-def]
    # Cast through Any so BrainClient accepts our fake that quacks like
    # AsyncAnthropic without inheriting from it. `settings` is unused
    # when `client=` is provided — the constructor only touches it for
    # the api_key kwarg on the real AsyncAnthropic.
    return BrainClient(
        settings=cast(Settings, None),
        client=cast(AsyncAnthropic, _FakeAnthropic(responder)),
    )


def _fake_http_error(status: int, body: str = "boom") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, text=body)
    return httpx.HTTPStatusError(body, request=req, response=resp)


# ─────────────────────── happy-path scenarios ─────────────────────────


class TestHappyPath:
    async def test_returns_brain_decision_from_valid_tool_block(self) -> None:
        tool_input = _valid_tool_input(
            decision="speak",
            priority="high",
            confidence=0.85,
            utterance="Why sharding here?",
        )

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={"type": "turn_end"}, t_ms=120_000)

        assert isinstance(decision, BrainDecision)
        assert decision.decision == "speak"
        assert decision.priority == "high"
        assert decision.confidence == 0.85
        assert decision.utterance == "Why sharding here?"
        assert decision.reason is None

    async def test_usage_populated_including_cost(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(
                tool_input=_valid_tool_input(),
                input_tokens=1000,
                output_tokens=200,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)

        assert decision.usage.input_tokens == 1000
        assert decision.usage.output_tokens == 200
        assert decision.usage.cost_usd == pytest.approx(
            estimate_cost_usd(
                model=BRAIN_MODEL,
                input_tokens=1000,
                output_tokens=200,
            )
        )

    async def test_cache_usage_summed_into_tokens_input_total(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(
                tool_input=_valid_tool_input(),
                input_tokens=100,
                output_tokens=30,
                cache_creation_input_tokens=200,
                cache_read_input_tokens=800,
            )

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)

        # Plan specifies:
        # tokens_input = input_tokens + (cache_creation or 0) + (cache_read or 0)
        assert decision.usage.tokens_input_total == 100 + 200 + 800
        # Cost reflects each category at its own rate.
        assert decision.usage.cost_usd == pytest.approx(
            estimate_cost_usd(
                model=BRAIN_MODEL,
                input_tokens=100,
                output_tokens=30,
                cache_creation_input_tokens=200,
                cache_read_input_tokens=800,
            )
        )

    async def test_none_cache_token_fields_default_to_zero(self) -> None:
        """Anthropic sometimes returns None for cache_* fields; the
        client must treat them as 0 not propagate the None through."""

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(
                tool_input=_valid_tool_input(),
                cache_creation_input_tokens=None,
                cache_read_input_tokens=None,
            )

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.usage.cache_creation_input_tokens == 0
        assert decision.usage.cache_read_input_tokens == 0

    async def test_messages_create_receives_expected_kwargs(self) -> None:
        captured: dict[str, Any] = {}

        def responder(kwargs: dict[str, Any]) -> Message:
            captured.update(kwargs)
            return _make_message(tool_input=_valid_tool_input())

        client = _client(responder)
        await client.decide(state=_state(), event={"type": "turn_end"}, t_ms=0)

        assert captured["model"] == BRAIN_MODEL
        assert captured["tool_choice"] == {
            "type": "tool",
            "name": "interview_decision",
        }
        # Cache marker on the system block is the only reason we split
        # static vs dynamic context.
        assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
        # Dynamic payload is on the user message, not the system block.
        assert captured["messages"][0]["role"] == "user"


# ─────────────────── degraded / stay_silent scenarios ─────────────────


class TestDegradedPaths:
    async def test_unexpected_stop_reason_returns_stay_silent(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(
                tool_input=_valid_tool_input(),
                stop_reason="end_turn",  # model ignored tool_choice
                include_tool_block=False,
            )

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "unexpected_stop_reason"
        # Usage must still be captured for cost accounting.
        assert decision.usage.input_tokens == 500

    async def test_missing_tool_block_returns_stay_silent(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            # stop_reason says tool_use but no tool block present.
            return _make_message(tool_input=None, include_tool_block=False)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "missing_tool_block"

    async def test_confidence_out_of_range_is_schema_violation(self) -> None:
        tool_input = _valid_tool_input(confidence=1.5)

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "schema_violation"
        # Raw input is preserved for debugging the prompt/schema drift.
        assert decision.raw_input == tool_input

    async def test_missing_required_reasoning_is_schema_violation(self) -> None:
        tool_input = {
            "decision": "stay_silent",
            "priority": "low",
            "confidence": 0.5,
        }

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.reason == "schema_violation"

    async def test_invalid_decision_enum_is_schema_violation(self) -> None:
        tool_input = _valid_tool_input(decision="shout")

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.reason == "schema_violation"

    async def test_non_dict_tool_input_is_schema_violation(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            # Build a message manually with a non-dict input (the SDK
            # allows this — its `input` is typed as object).
            msg = _make_message(tool_input=_valid_tool_input())
            # Mutate the tool block to carry a list instead of a dict.
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    # SDK types `input` as `dict[str, object]`; Claude
                    # sometimes violates this at runtime. Cast through
                    # Any so ty accepts the deliberate type abuse.
                    block.input = cast(Any, ["not", "a", "dict"])
            return msg

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.reason == "schema_violation"

    async def test_long_utterance_degrades_to_stay_silent(self) -> None:
        tool_input = _valid_tool_input(
            decision="speak",
            priority="medium",
            confidence=0.7,
            utterance="x" * 1200,
        )

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "utterance_too_long"

    async def test_control_char_utterance_degrades_to_stay_silent(self) -> None:
        tool_input = _valid_tool_input(
            decision="speak",
            priority="medium",
            confidence=0.7,
            utterance="ok\x00injection",
        )

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "utterance_has_control_chars"


# ─────────────────────── error taxonomy ───────────────────────────────


class TestErrorTaxonomy:
    async def test_authentication_error_propagates(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            raise anthropic.AuthenticationError(
                message="bad key",
                response=_fake_http_error(401).response,
                body=None,
            )

        client = _client(responder)
        with pytest.raises(anthropic.AuthenticationError):
            await client.decide(state=_state(), event={}, t_ms=0)

    async def test_bad_request_error_propagates(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            raise anthropic.BadRequestError(
                message="invalid",
                response=_fake_http_error(400).response,
                body=None,
            )

        client = _client(responder)
        with pytest.raises(anthropic.BadRequestError):
            await client.decide(state=_state(), event={}, t_ms=0)

    async def test_rate_limit_error_degrades_to_stay_silent(self) -> None:
        """SDK's built-in retry (max_retries=2) is in front of our fake;
        by the time a RateLimitError reaches us it's already post-retry,
        so we degrade rather than re-raise."""

        def responder(_k: dict[str, Any]) -> Message:
            raise anthropic.RateLimitError(
                message="slow down",
                response=_fake_http_error(429).response,
                body=None,
            )

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "api_error"

    async def test_api_connection_error_degrades_to_stay_silent(self) -> None:
        def responder(_k: dict[str, Any]) -> Message:
            raise anthropic.APIConnectionError(
                message="boom",
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            )

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.reason == "api_error"

    async def test_cancelled_error_propagates(self) -> None:
        """Router relies on CancelledError propagating to abort in-flight
        brain calls when the candidate starts speaking. Swallowing it
        would wedge the router."""

        def responder(_k: dict[str, Any]) -> Message:
            raise asyncio.CancelledError()

        client = _client(responder)
        with pytest.raises(asyncio.CancelledError):
            await client.decide(state=_state(), event={}, t_ms=0)

    async def test_brain_timeout_degrades_to_stay_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R16: when the SDK retry chain stalls past `_BRAIN_DEADLINE_S`
        the wrap fires `asyncio.TimeoutError` and we degrade with
        `reason="brain_timeout"`. Router's `_dispatch` then routes that
        through R27's synthetic-recovery emitter."""
        # Squeeze the deadline so the test runs fast; the real value is
        # 180 s.
        monkeypatch.setattr("archmentor_agent.brain.client._BRAIN_DEADLINE_S", 0.05)

        async def hang(_k: dict[str, Any]) -> Message:
            await asyncio.sleep(10)
            raise AssertionError("should not reach")

        client = _client(hang)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "brain_timeout"

    async def test_external_cancel_propagates_over_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Router's `cancel_in_flight()` cancels the wrapping task; the
        wait_for wrapper must NOT shadow that as a TimeoutError. Invariant
        I2 (re-prepend pending batch on cancel) depends on this."""
        # Long deadline so the test relies on the external cancel, not
        # the timeout.
        monkeypatch.setattr("archmentor_agent.brain.client._BRAIN_DEADLINE_S", 30.0)

        async def hang(_k: dict[str, Any]) -> Message:
            await asyncio.sleep(10)
            raise AssertionError("should not reach")

        client = _client(hang)
        task = asyncio.create_task(client.decide(state=_state(), event={}, t_ms=0))
        # Yield once so the wait_for-wrapped coroutine starts.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
