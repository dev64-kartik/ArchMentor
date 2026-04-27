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
from _helpers import FakeAsyncMessageStream, utterance_deltas
from _helpers.streaming import FakeStreamEvent
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
    def __init__(self, responder, stream_responder=None):  # type: ignore[no-untyped-def]
        self._responder = responder
        self._stream_responder = stream_responder
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        result = self._responder(kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    def stream(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        # `stream(...)` does NOT await — it returns the manager
        # synchronously, then the caller does `async with`. This
        # mirrors the real SDK's `AsyncMessageStreamManager` shape.
        self.stream_calls.append(kwargs)
        if self._stream_responder is None:
            raise AssertionError(
                "_FakeMessages.stream called but no stream_responder configured; "
                "tests that exercise the streaming path must pass `stream_responder=`."
            )
        return self._stream_responder(kwargs)


class _FakeAnthropic:
    """Satisfies the subset of AsyncAnthropic surface BrainClient uses.

    We only need `messages.create(...)`, `messages.stream(...)`, and
    `close()`. The real SDK has retry middleware in front of
    `messages.create`; we test post-retry behavior by having the fake
    raise the post-retry exception directly. This mirrors the ledger-
    client test approach where `httpx.MockTransport` stands in for the
    real transport.
    """

    def __init__(self, responder, stream_responder=None):  # type: ignore[no-untyped-def]
        self.messages = _FakeMessages(responder, stream_responder)

    async def close(self) -> None:
        return None


def _client(responder, stream_responder=None) -> BrainClient:  # type: ignore[no-untyped-def]
    # Cast through Any so BrainClient accepts our fake that quacks like
    # AsyncAnthropic without inheriting from it. `settings` is unused
    # when `client=` is provided — the constructor only touches it for
    # the api_key kwarg on the real AsyncAnthropic.
    return BrainClient(
        settings=cast(Settings, None),
        client=cast(AsyncAnthropic, _FakeAnthropic(responder, stream_responder)),
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


class TestXmlToolInputRecovery:
    """Opus 4.x intermittently emits nested-object fields as the literal
    `<parameter name="...">value</parameter>` XML string instead of a
    JSON object. Observed in the M3 dogfood (2026-04-25): the brain had
    a real session_summary update to land but the wrapper format failed
    schema validation and the entire dispatch was lost. The recovery
    path inflates the XML back into the dict shape the schema expects.
    """

    async def test_state_updates_xml_string_is_recovered(self) -> None:
        xml_blob = (
            '\n<parameter name="session_summary_append">Candidate asked '
            "&quot;is it the right call?&quot; — fishing for validation."
            "</parameter>"
        )
        tool_input = _valid_tool_input()
        tool_input["state_updates"] = xml_blob

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)

        # Schema validation passed because state_updates was reshaped.
        assert decision.reason is None
        assert decision.state_updates == {
            "session_summary_append": (
                "Candidate asked &quot;is it the right call?&quot; — fishing for validation."
            )
        }

    async def test_state_updates_xml_with_multiple_fields_is_recovered(
        self,
    ) -> None:
        xml_blob = (
            '<parameter name="session_summary_append">Pushed for QPS.</parameter>'
            '<parameter name="phase_advance">capacity</parameter>'
        )
        tool_input = _valid_tool_input()
        tool_input["state_updates"] = xml_blob

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.reason is None
        assert decision.state_updates == {
            "session_summary_append": "Pushed for QPS.",
            "phase_advance": "capacity",
        }

    async def test_unrecognizable_string_still_fails_validation(self) -> None:
        """If the offending string isn't XML-shaped we don't pretend to
        recover — it falls through to the existing schema_violation
        path so the operator still sees a warning."""
        tool_input = _valid_tool_input()
        tool_input["state_updates"] = "just a regular sentence"

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=tool_input)

        client = _client(responder)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "schema_violation"

    def test_oversized_input_returns_none_without_scanning(self) -> None:
        """#38: inputs larger than 32 KiB must return None immediately,
        guarding against ReDoS on adversarially-crafted API responses."""
        from archmentor_agent.brain.client import _recover_xml_state_updates

        # 100 KiB of valid-looking XML that would take a long time to
        # scan if the regex ran. We only need to assert it returns None
        # (the cap fires) — not that it's fast (that's hard to unit-test
        # reliably in CI). Correctness is sufficient here.
        big_blob = '<parameter name="k">' + ("x" * (100 * 1024)) + "</parameter>"
        result = _recover_xml_state_updates(big_blob)
        assert result is None, "expected None for oversized input"


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

    async def test_api_connection_error_before_deadline_propagates(self) -> None:
        """APIConnectionError arriving before the deadline is a genuine network
        failure and must propagate (not silently coerced to stay_silent).

        Note: Fix 5 changed the behavior of this scenario from `api_error`
        (silent degrade) to `propagate` because the router's generic exception
        handler (`except Exception`) will catch it and log `router.brain.unexpected`.
        This is the correct signal for an unexpected connection error that
        isn't deadline-triggered.
        """

        def responder(_k: dict[str, Any]) -> Message:
            raise anthropic.APIConnectionError(
                message="genuine connection error before deadline",
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            )

        client = _client(responder)
        with pytest.raises(anthropic.APIConnectionError):
            await client.decide(state=_state(), event={}, t_ms=0)

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

    async def test_api_connection_error_after_deadline_returns_discriminated_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SDK converts wait_for's CancelledError → APIConnectionError mid-backoff.

        When the wall-clock has elapsed past _BRAIN_DEADLINE_S at the time the
        APIConnectionError arrives, we treat it as a deadline-triggered abort
        and return stay_silent("anthropic_api_connection_during_wait_for") so
        R27's synthetic recovery utterance fires.

        Test strategy: patch `asyncio.get_event_loop().time` so that when the
        elapsed-time check runs inside `decide`, it sees a value >= deadline,
        even though no real time has passed. This simulates the production
        scenario where the SDK blocks past the deadline without yielding back
        through asyncio (e.g. inside its own blocking retry logic).
        """
        import archmentor_agent.brain.client as _brain_client_module

        deadline = 180.0
        monkeypatch.setattr(_brain_client_module, "_BRAIN_DEADLINE_S", deadline)

        # We can't use asyncio.sleep inside the fake (wait_for would intercept
        # it). Instead we raise APIConnectionError immediately but monkeypatch
        # asyncio.get_event_loop().time so the elapsed check reads >= deadline.
        # Using patched event-loop time simulates "SDK raised after the deadline
        # elapsed" without any real wall-clock wait.
        call_count = 0

        def patched_time() -> float:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call is `call_start_s = event_loop.time()`.
                return 0.0
            # Second call is the elapsed check after the exception —
            # return a value past the deadline.
            return deadline + 1.0

        monkeypatch.setattr(asyncio.get_event_loop(), "time", patched_time)

        def raise_connection_error(_k: dict[str, Any]) -> Message:
            raise anthropic.APIConnectionError(
                message="connection aborted mid-backoff",
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            )

        client = _client(raise_connection_error)
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"
        assert decision.reason == "anthropic_api_connection_during_wait_for"

    async def test_external_cancel_not_swallowed_by_api_connection_branch(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CancelledError must not be accidentally caught by the APIConnectionError branch.

        This verifies the Fix 5 discriminated branch still lets external
        cancellation propagate correctly (invariant I2 — router cancellation
        contract must not be broken).
        """
        monkeypatch.setattr("archmentor_agent.brain.client._BRAIN_DEADLINE_S", 30.0)

        async def hang(_k: dict[str, Any]) -> Message:
            await asyncio.sleep(10)
            raise AssertionError("should not reach")

        client = _client(hang)
        task = asyncio.create_task(client.decide(state=_state(), event={}, t_ms=0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ─────────────────────── streaming-path scenarios ─────────────────────


class TestStreamingPath:
    """Per M4 plan Unit 3 — streaming `BrainClient.decide` reshapes the
    call to use `messages.stream(...)`, surfaces `utterance` deltas to a
    listener, and validates the full `tool_use.input` after `message_stop`.

    When `utterance_listener is None`, `decide()` falls through to the
    legacy `_decide_blocking` path so replay (`scripts/replay.py`)
    determinism is preserved.
    """

    async def test_listener_receives_each_utterance_delta(self) -> None:
        utterance = "Walk me through your capacity assumptions."
        events = utterance_deltas(utterance, chunks=4)
        final_msg = _make_message(
            tool_input=_valid_tool_input(
                decision="speak",
                priority="high",
                confidence=0.85,
                utterance=utterance,
            ),
        )

        def stream_responder(_kwargs: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(events=events, final_message=final_msg)

        client = _client(responder=lambda _k: final_msg, stream_responder=stream_responder)

        deltas: list[str] = []

        async def listener(delta: str) -> None:
            deltas.append(delta)

        decision = await client.decide(
            state=_state(),
            event={"type": "turn_end"},
            t_ms=0,
            utterance_listener=listener,
        )

        assert decision.decision == "speak"
        assert decision.utterance == utterance
        # Concatenated deltas reconstruct the full utterance.
        assert "".join(deltas) == utterance
        # No empty deltas (each event added new characters).
        assert all(d for d in deltas)

    async def test_no_listener_falls_through_to_blocking_path(self) -> None:
        """Replay determinism: `decide()` without a listener still runs
        the non-streaming `messages.create` path. No `stream_responder`
        configured here — `_FakeMessages.stream` would assert if hit."""

        def responder(_k: dict[str, Any]) -> Message:
            return _make_message(tool_input=_valid_tool_input())

        client = _client(responder)  # no stream_responder
        decision = await client.decide(state=_state(), event={}, t_ms=0)
        assert decision.decision == "stay_silent"

    async def test_stay_silent_decision_never_invokes_listener(self) -> None:
        """When `utterance` is null/missing throughout the stream, the
        listener is never called."""
        # Snapshot reveals only `decision`/`reasoning`, never `utterance`.
        events = [
            FakeStreamEvent(
                type="input_json",
                snapshot={"reasoning": "ok"},
            ),
            FakeStreamEvent(
                type="input_json",
                snapshot={"reasoning": "ok", "decision": "stay_silent"},
            ),
        ]
        final_msg = _make_message(tool_input=_valid_tool_input())

        def stream_responder(_kwargs: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(events=events, final_message=final_msg)

        client = _client(responder=lambda _k: final_msg, stream_responder=stream_responder)
        called = False

        async def listener(_delta: str) -> None:
            nonlocal called
            called = True

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.decision == "stay_silent"
        assert decision.reason is None
        assert called is False

    async def test_utterance_below_min_chars_skipped_until_two_chars(self) -> None:
        """Single-char `utterance: "W"` snapshot should not trigger the
        listener — the floor is 2 chars to dodge mid-token noise. Once
        the snapshot grows past the floor, the full prefix is delivered
        in one delta."""
        events = [
            FakeStreamEvent(type="input_json", snapshot={"utterance": "W"}),
            FakeStreamEvent(type="input_json", snapshot={"utterance": "Wal"}),
            FakeStreamEvent(type="input_json", snapshot={"utterance": "Walk."}),
        ]
        final_msg = _make_message(
            tool_input=_valid_tool_input(decision="speak", utterance="Walk."),
        )

        def stream_responder(_k: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(events=events, final_message=final_msg)

        client = _client(responder=lambda _k: final_msg, stream_responder=stream_responder)
        deltas: list[str] = []

        async def listener(d: str) -> None:
            deltas.append(d)

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.utterance == "Walk."
        assert "".join(deltas) == "Walk."
        # First delta is "Wal" (3 chars) not "W" — floor skipped one event.
        assert deltas[0] == "Wal"

    async def test_control_char_utterance_rejected_post_stream(self) -> None:
        """Final validation runs `sanitize_utterance` after `message_stop`;
        a control char in the final input flips reason to
        `utterance_has_control_chars`. The half-utterance the listener
        already heard is NOT rolled back — by design, per M4 plan R4."""
        utterance_with_ctrl = "ok\x00injection"
        events = [
            FakeStreamEvent(type="input_json", snapshot={"utterance": "ok"}),
            FakeStreamEvent(
                type="input_json",
                snapshot={"utterance": utterance_with_ctrl},
            ),
        ]
        final_msg = _make_message(
            tool_input=_valid_tool_input(
                decision="speak",
                utterance=utterance_with_ctrl,
            ),
        )

        def stream_responder(_k: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(events=events, final_message=final_msg)

        client = _client(responder=lambda _k: final_msg, stream_responder=stream_responder)
        deltas: list[str] = []

        async def listener(d: str) -> None:
            deltas.append(d)

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.decision == "stay_silent"
        assert decision.reason == "utterance_has_control_chars"
        # Listener still saw the partial pre-control-char text.
        assert deltas[0] == "ok"

    async def test_aenter_authentication_error_propagates(self) -> None:
        def stream_responder(_k: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(
                aenter_error=anthropic.AuthenticationError(
                    message="bad key",
                    response=_fake_http_error(401).response,
                    body=None,
                ),
            )

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)

        async def listener(_d: str) -> None:
            pass

        with pytest.raises(anthropic.AuthenticationError):
            await client.decide(state=_state(), event={}, t_ms=0, utterance_listener=listener)

    async def test_aenter_rate_limit_error_degrades_to_stay_silent(self) -> None:
        def stream_responder(_k: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(
                aenter_error=anthropic.RateLimitError(
                    message="slow down",
                    response=_fake_http_error(429).response,
                    body=None,
                ),
            )

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)

        async def listener(_d: str) -> None:
            pass

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.decision == "stay_silent"
        assert decision.reason == "api_error"

    async def test_mid_stream_api_connection_error_degrades(self) -> None:
        events = utterance_deltas("Walk me through capacity.", chunks=2)

        def stream_responder(_k: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(
                events=events,
                aiter_error=anthropic.APIConnectionError(
                    message="dropped mid-stream",
                    request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
                ),
                aiter_error_after=1,  # error fires after first event yields
            )

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)
        deltas: list[str] = []

        async def listener(d: str) -> None:
            deltas.append(d)

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.decision == "stay_silent"
        assert decision.reason == "api_error"
        # First delta still reached the listener before the error.
        assert deltas, "listener should have received at least one delta"

    async def test_stream_timeout_degrades_to_brain_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`asyncio.wait_for` deadline trips → `stay_silent("brain_timeout")`
        so R27's synthetic recovery utterance fires (router-side path
        unchanged from M2)."""
        monkeypatch.setattr("archmentor_agent.brain.client._BRAIN_DEADLINE_S", 0.05)

        class _HangingStream:
            async def __aenter__(self) -> _HangingStream:
                await asyncio.sleep(10)
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def __aiter__(self):
                if False:  # pragma: no cover
                    yield None

            async def get_final_message(self) -> Any:
                raise AssertionError("should not reach")

        def stream_responder(_k: dict[str, Any]) -> _HangingStream:
            return _HangingStream()

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)

        async def listener(_d: str) -> None:
            pass

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.decision == "stay_silent"
        assert decision.reason == "brain_timeout"

    async def test_external_cancel_propagates_through_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Router invariant I2 — `cancel_in_flight()` must propagate
        `CancelledError` through the streaming `async with` exit; the
        wait_for wrapper must NOT shadow it as a TimeoutError."""
        monkeypatch.setattr("archmentor_agent.brain.client._BRAIN_DEADLINE_S", 30.0)

        class _HangingStream:
            async def __aenter__(self) -> _HangingStream:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def __aiter__(self):
                await asyncio.sleep(10)
                if False:  # pragma: no cover
                    yield None

            async def get_final_message(self) -> Any:
                raise AssertionError("should not reach")

        def stream_responder(_k: dict[str, Any]) -> _HangingStream:
            return _HangingStream()

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)

        async def listener(_d: str) -> None:
            pass

        task = asyncio.create_task(
            client.decide(state=_state(), event={}, t_ms=0, utterance_listener=listener)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_post_stream_schema_violation(self) -> None:
        """Final `tool_use.input` fails jsonschema → `schema_violation`.
        Listener may have seen partial utterance audio already; that's
        fine — by design the audio is not rolled back (M4 plan R4)."""
        # Stream emits a valid utterance; final message has confidence
        # out of range → schema violation.
        utterance = "Walk me through capacity."
        events = utterance_deltas(utterance, chunks=2)
        bad_input = _valid_tool_input(decision="speak", utterance=utterance, confidence=1.5)
        final_msg = _make_message(tool_input=bad_input)

        def stream_responder(_k: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(events=events, final_message=final_msg)

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)
        deltas: list[str] = []

        async def listener(d: str) -> None:
            deltas.append(d)

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.decision == "stay_silent"
        assert decision.reason == "schema_violation"
        # Audio already played to listener; not rolled back.
        assert "".join(deltas) == utterance

    async def test_post_stream_xml_tool_input_recovered(self) -> None:
        """XML-spillover recovery runs only on the final accumulated dict,
        per M4 plan R4 (never on partial snapshots)."""
        utterance = "Pushed for QPS."
        events = utterance_deltas(utterance, chunks=2)
        tool_input = _valid_tool_input(
            decision="speak",
            utterance=utterance,
        )
        tool_input["state_updates"] = (
            '<parameter name="session_summary_append">Pushed for QPS.</parameter>'
        )
        final_msg = _make_message(tool_input=tool_input)

        def stream_responder(_k: dict[str, Any]) -> FakeAsyncMessageStream:
            return FakeAsyncMessageStream(events=events, final_message=final_msg)

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)

        async def listener(_d: str) -> None:
            pass

        decision = await client.decide(
            state=_state(), event={}, t_ms=0, utterance_listener=listener
        )
        assert decision.reason is None
        assert decision.state_updates == {
            "session_summary_append": "Pushed for QPS.",
        }

    async def test_stream_kwargs_match_blocking_path(self) -> None:
        """The streaming path must pass identical kwargs to `messages.stream`
        as the blocking path passes to `messages.create`. Replay determinism
        depends on this — the only difference between the two paths is the
        SDK entry point, not the prompt or tool config."""
        captured: dict[str, Any] = {}
        utterance = "Hi."
        final_msg = _make_message(
            tool_input=_valid_tool_input(decision="speak", utterance=utterance),
        )

        def stream_responder(kwargs: dict[str, Any]) -> FakeAsyncMessageStream:
            captured.update(kwargs)
            return FakeAsyncMessageStream(
                events=utterance_deltas(utterance, chunks=1),
                final_message=final_msg,
            )

        client = _client(responder=lambda _k: None, stream_responder=stream_responder)

        async def listener(_d: str) -> None:
            pass

        await client.decide(
            state=_state(),
            event={"type": "turn_end"},
            t_ms=0,
            utterance_listener=listener,
        )
        assert captured["model"] == BRAIN_MODEL
        assert captured["tool_choice"] == {
            "type": "tool",
            "name": "interview_decision",
        }
        assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}


class TestToolSchemaPropertyOrder:
    """M4 plan R6: `utterance` must be the first property declared in
    `INTERVIEW_DECISION_TOOL["input_schema"]["properties"]` so Anthropic
    streams it first and TTS can start as soon as the model commits to
    speaking."""

    def test_utterance_is_first_property(self) -> None:
        from archmentor_agent.brain.tools import INTERVIEW_DECISION_TOOL

        first_key = next(iter(INTERVIEW_DECISION_TOOL["input_schema"]["properties"]))
        assert first_key == "utterance", (
            f"`utterance` must be declared first to minimize TTFA; got `{first_key}`"
        )

    def test_state_updates_is_last_property(self) -> None:
        from archmentor_agent.brain.tools import INTERVIEW_DECISION_TOOL

        keys = list(INTERVIEW_DECISION_TOOL["input_schema"]["properties"])
        assert keys[-1] == "state_updates"


class TestBrainDecisionPartialFactory:
    """M4 plan R3c: `BrainDecision.partial(utterance, reason)` is the
    explicit factory for cancellation post-utterance pre-`message_stop`.
    Combination `decision="stay_silent" + reason=<...> + partial=True`
    is the three-part discriminator M5/M6 replay readers look for."""

    def test_partial_factory_carries_utterance_and_partial_flag(self) -> None:
        decision = BrainDecision.partial(
            utterance="Walk me through capacity.",
            reason="cancelled_mid_stream",
        )
        assert decision.decision == "stay_silent"
        assert decision.utterance == "Walk me through capacity."
        assert decision.reason == "cancelled_mid_stream"
        assert decision.is_partial is True

    def test_other_factories_default_partial_false(self) -> None:
        for d in (
            BrainDecision.stay_silent("anything"),
            BrainDecision.schema_violation(None),
            BrainDecision.cost_capped(),
            BrainDecision.skipped_idempotent(),
            BrainDecision.skipped_cooldown(cooldown_ms=4_000),
        ):
            assert d.is_partial is False, f"{d.reason} should default is_partial=False"
