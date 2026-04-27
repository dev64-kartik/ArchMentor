"""Non-streaming Anthropic tool-use client for the interview brain.

Single responsibility: take `SessionState + event payload`, call
Anthropic with `tool_choice` forcing `interview_decision`, validate the
returned tool_use.input against `INTERVIEW_DECISION_TOOL["input_schema"]`,
and return a `BrainDecision`.

Cost guard lives in the event router, NOT here (see plan's "Key
Technical Decisions"). This client never inspects
`state.cost_usd_total` — it only reports the delta for one call.

The client must let `asyncio.CancelledError` propagate so the router's
`cancel_in_flight()` path actually aborts the brain call when the
candidate starts speaking again. Catching bare `Exception` inside
`decide(...)` is a footgun — do not do it.

Logging vocabulary:

- `brain.call.begin`  — dispatching an Anthropic request
- `brain.call.end`    — got a response (includes usage + cost fields)
- `brain.schema_violation`  — tool_use.input failed jsonschema
- `brain.unexpected_stop`   — stop_reason != "tool_use"
- `brain.utterance_rejected`— sanitizer dropped the utterance
- `brain.api_error`         — retriable error, after SDK retries
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock, Usage
from jsonschema import Draft202012Validator

from archmentor_agent.brain.decision import BrainDecision, BrainUsage
from archmentor_agent.brain.pricing import BRAIN_MODEL, estimate_cost_usd
from archmentor_agent.brain.prompt_builder import build_call_kwargs
from archmentor_agent.brain.tools import INTERVIEW_DECISION_TOOL
from archmentor_agent.config import Settings, get_settings
from archmentor_agent.state import SessionState

log = structlog.get_logger(__name__)

# Type alias for `BrainClient.decide(utterance_listener=...)`. Each call
# delivers the *new portion* of the streamed `utterance` field — the
# substring that wasn't visible in the previous snapshot. The listener
# is awaited synchronously inside the stream loop, so it should be cheap
# (e.g. push into a `livekit-agents` SynthesizeStream).
UtteranceListener = Callable[[str], Awaitable[None]]

_MAX_TOKENS = 1024

# Floor before the streaming utterance listener is invoked. Single-char
# snapshots can be partial-token noise from the JSON parser; waiting
# until the prefix is at least 2 chars dodges most of that without
# materially delaying TTS (Kokoro on Apple Silicon's per-call inference
# is itself the dominant cost). M4 plan, Unit 3 deferred-to-implementation
# note on first-sentence minimum length.
_UTTERANCE_LISTENER_MIN_CHARS = 2

# M4 R3d — closing fragment pushed through the streaming TTS when
# post-stream validation rejects the tool_use input after partial audio
# has played. The surrounding "—" matches M3 R27/R28 voice and the lower
# minimum sentence length so the SentenceTokenizer flushes promptly.
_SCHEMA_VIOLATION_TAIL = " — actually, let me think again."

# Bounds a single Anthropic call. Without this, the SDK default of 600 s
# combined with `max_retries=2` (= 3 total attempts) would let a hung
# gateway hold the router's serialization gate for ~30 min. Opus
# latency observed in M2 is 7-15 s per call, so a 120 s read timeout
# leaves ~100 s headroom. `connect`/`write`/`pool` are tight because
# those phases shouldn't need more than a few seconds on any healthy
# network.
_BRAIN_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

# Total wall-clock budget for one `decide()` call across the SDK's
# retry chain. The httpx-level read timeout above bounds a single
# attempt at 120 s; with `max_retries=2` the chain can stretch to ~360 s
# of compounded backoff, which would hold the router's serialization
# gate well past any reasonable interview cadence. 180 s gives one
# full attempt + headroom for at most one cheap retry. On exhaustion we
# return `BrainDecision.stay_silent("brain_timeout")` so the voice loop
# survives — `asyncio.CancelledError` MUST still propagate (router
# cancellation contract — see `test_cancelled_error_propagates`).
_BRAIN_DEADLINE_S = 180.0

# `Draft202012Validator(schema)` is compiled once per process; reusing
# it avoids re-parsing the schema on every brain call. The schema is a
# static TypedDict literal, so sharing a validator is safe.
_DECISION_VALIDATOR = Draft202012Validator(
    dict(INTERVIEW_DECISION_TOOL["input_schema"]),
)


class BrainClient:
    """Wraps `AsyncAnthropic` for the `interview_decision` tool call."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncAnthropic | None = None,
        model: str = BRAIN_MODEL,
    ) -> None:
        # `client` is the test seam — tests pass a pre-built
        # AsyncAnthropic with a mocked transport. Production callers
        # omit it and we build one from settings.
        self._model = model
        # `base_url=None` is the SDK's own default (routes to
        # api.anthropic.com). Passing it through unconditionally keeps
        # the constructor behaviour identical to the pre-gateway path
        # when no override is set.
        self._client = client or AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            base_url=settings.anthropic_base_url,
            # SDK-level retry covers 429 + 5xx + APIConnectionError
            # with exponential backoff. 2 retries = 3 total attempts.
            max_retries=2,
            timeout=_BRAIN_TIMEOUT,
        )

    async def decide(
        self,
        *,
        state: SessionState,
        event: dict[str, Any],
        t_ms: int,
        utterance_listener: UtteranceListener | None = None,
    ) -> BrainDecision:
        """Run one brain call and return a `BrainDecision`.

        Never raises on Anthropic 5xx / rate-limit / transport errors
        (after SDK retries) — those degrade to `stay_silent` so the
        voice loop keeps running. 4xx auth/bad-request DO raise:
        they're bugs the operator needs to see.

        `asyncio.CancelledError` propagates. The router relies on it
        to abort the call when the candidate starts speaking.

        When `utterance_listener` is provided, the streaming path
        (`messages.stream`) is used and each new portion of the
        `utterance` field is awaited on the listener as soon as the
        SDK surfaces it — this is the production live-session flow,
        used to drive sentence-chunked Kokoro TTS (M4 Unit 4).

        When `utterance_listener is None`, the legacy blocking path
        (`messages.create`) is used; this keeps `scripts/replay.py`
        deterministic relative to the M2/M3 era and simplifies tests
        that don't care about streaming semantics. See M4 plan Unit 3.
        """
        if utterance_listener is None:
            return await self._decide_blocking(state=state, event=event, t_ms=t_ms)
        return await self._decide_streaming(
            state=state,
            event=event,
            t_ms=t_ms,
            utterance_listener=utterance_listener,
        )

    async def _decide_blocking(
        self,
        *,
        state: SessionState,
        event: dict[str, Any],
        t_ms: int,
    ) -> BrainDecision:
        """Non-streaming `messages.create` path. Reached from
        `scripts/replay.py` and from any caller that doesn't pass an
        `utterance_listener`. Behaviour unchanged from M2/M3."""
        kwargs = build_call_kwargs(
            state,
            event,
            model=self._model,
            tool=INTERVIEW_DECISION_TOOL,
            max_tokens=_MAX_TOKENS,
        )

        log.info(
            "brain.call.begin",
            t_ms=t_ms,
            model=self._model,
            transcript_turns=len(state.transcript_window),
            decisions=len(state.decisions),
            phase=state.phase.value,
            mode="blocking",
        )

        call_start_s = asyncio.get_event_loop().time()

        try:
            response: Message = await asyncio.wait_for(
                self._client.messages.create(**kwargs),
                timeout=_BRAIN_DEADLINE_S,
            )
        except TimeoutError:
            # `wait_for` cancels the inner coroutine, which the SDK
            # converts to a clean abort. We degrade rather than raise so
            # the voice loop keeps running; the router's `_dispatch`
            # surfaces `reason="brain_timeout"` to the synthetic-recovery
            # emitter (R27).
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind="TimeoutError",
                deadline_s=_BRAIN_DEADLINE_S,
            )
            return BrainDecision.stay_silent("brain_timeout")
        except (anthropic.BadRequestError, anthropic.AuthenticationError) as exc:
            # These are bugs (bad request shape, invalid API key), not
            # runtime failures. Surface them loudly so the session
            # errors out rather than silently staying mute forever.
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind=type(exc).__name__,
                status=getattr(exc, "status_code", None),
            )
            raise
        except anthropic.APIConnectionError as exc:
            # SDK behaviour assumption (verified against anthropic-sdk-python
            # ≥0.28 with httpx transport, 2026-04):
            #
            # When `asyncio.wait_for` fires while the SDK is mid-backoff
            # between retries, the underlying `asyncio.sleep` in the SDK's
            # retry loop is cancelled by wait_for's internal cancel. The SDK
            # intercepts the CancelledError as part of its network-error
            # handling and converts it to `APIConnectionError` before
            # re-raising, so wait_for never sees its own TimeoutError.
            # The observable effect: R27's "brain_timeout" synthetic recovery
            # utterance would never fire because the router's
            # `_maybe_emit_recovery_utterance` checks for `reason == "brain_timeout"`.
            #
            # Fix: when an APIConnectionError arrives and the wall-clock has
            # elapsed past `_BRAIN_DEADLINE_S`, treat it as a deadline-
            # triggered abort and return a discriminated `stay_silent` reason.
            # When the error arrives WITHIN the deadline (a genuine mid-session
            # connection drop before the SDK exhausted its retries), propagate
            # so the router's generic `except Exception` path logs and degrades.
            #
            # Verification note: if R27 fails to fire on production timeouts,
            # check the SDK version's retry logic in
            # `anthropic/_base_client.py::BaseClient._should_retry` and
            # `anthropic/_utils/retry.py`. The conversion of CancelledError →
            # APIConnectionError must still be happening in the newer SDK for
            # this branch to matter; if the SDK lets CancelledError propagate
            # transparently the `except TimeoutError` branch above already
            # handles it correctly.
            elapsed_s = asyncio.get_event_loop().time() - call_start_s
            if elapsed_s >= _BRAIN_DEADLINE_S:
                log.error(
                    "brain.api_error",
                    t_ms=t_ms,
                    kind="APIConnectionError_after_deadline",
                    elapsed_s=elapsed_s,
                    deadline_s=_BRAIN_DEADLINE_S,
                )
                return BrainDecision.stay_silent("anthropic_api_connection_during_wait_for")
            # Connection error BEFORE the deadline — a genuine mid-call
            # network failure, not a deadline-triggered cancel. Let it
            # propagate; the router's generic handler will degrade to
            # stay_silent("brain_unexpected") and log the exception.
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind=type(exc).__name__,
                elapsed_s=elapsed_s,
            )
            raise
        except (
            anthropic.RateLimitError,
            anthropic.APIStatusError,
        ) as exc:
            # SDK already retried (max_retries=2). Treat final failure
            # as "voice loop survives, mentor stays quiet."
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind=type(exc).__name__,
                status=getattr(exc, "status_code", None),
            )
            return BrainDecision.stay_silent("api_error")

        usage = _usage_from_response(response.usage, model=self._model)
        return _assemble_decision_from_message(response, t_ms=t_ms, usage=usage)

    async def _decide_streaming(
        self,
        *,
        state: SessionState,
        event: dict[str, Any],
        t_ms: int,
        utterance_listener: UtteranceListener,
    ) -> BrainDecision:
        """Streaming `messages.stream` path used in live sessions.

        Each new portion of the `utterance` field is awaited on
        `utterance_listener` as soon as the SDK surfaces it. After
        `message_stop`, the accumulated `tool_use.input` is validated
        against the same jsonschema + XML-spillover recovery + utterance
        sanitiser the blocking path uses; the resulting `BrainDecision`
        has the same shape regardless of which entry point produced it.

        Streaming-error decision matrix (M4 plan §405-422):
        - 4xx (Auth/BadRequest) → propagate (caller bug).
        - 5xx / RateLimit → degrade to `stay_silent("api_error")`.
        - APIConnectionError after `_BRAIN_DEADLINE_S` →
          `stay_silent("anthropic_api_connection_during_wait_for")`.
        - APIConnectionError within deadline → propagate (router catches).
        - `asyncio.TimeoutError` → `stay_silent("brain_timeout")` (R27 fires).
        - `CancelledError` → propagate (router invariant I2).
        - Final-validation failure → `BrainDecision.schema_violation(...)`.

        Partial TTS audio that already played is NOT rolled back when
        post-stream validation fails; the audio is in the candidate's
        ear and the snapshot row carries the discriminator.
        """
        kwargs = build_call_kwargs(
            state,
            event,
            model=self._model,
            tool=INTERVIEW_DECISION_TOOL,
            max_tokens=_MAX_TOKENS,
        )

        log.info(
            "brain.call.begin",
            t_ms=t_ms,
            model=self._model,
            transcript_turns=len(state.transcript_window),
            decisions=len(state.decisions),
            phase=state.phase.value,
            mode="streaming",
        )

        call_start_s = asyncio.get_event_loop().time()

        try:
            return await asyncio.wait_for(
                self._stream_and_assemble(
                    kwargs=kwargs,
                    t_ms=t_ms,
                    utterance_listener=utterance_listener,
                ),
                timeout=_BRAIN_DEADLINE_S,
            )
        except TimeoutError:
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind="TimeoutError",
                deadline_s=_BRAIN_DEADLINE_S,
                mode="streaming",
            )
            return BrainDecision.stay_silent("brain_timeout")
        except (anthropic.BadRequestError, anthropic.AuthenticationError) as exc:
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind=type(exc).__name__,
                status=getattr(exc, "status_code", None),
                mode="streaming",
            )
            raise
        except anthropic.APIConnectionError as exc:
            elapsed_s = asyncio.get_event_loop().time() - call_start_s
            if elapsed_s >= _BRAIN_DEADLINE_S:
                log.error(
                    "brain.api_error",
                    t_ms=t_ms,
                    kind="APIConnectionError_after_deadline",
                    elapsed_s=elapsed_s,
                    deadline_s=_BRAIN_DEADLINE_S,
                    mode="streaming",
                )
                return BrainDecision.stay_silent("anthropic_api_connection_during_wait_for")
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind=type(exc).__name__,
                elapsed_s=elapsed_s,
                mode="streaming",
            )
            # Mid-stream connection drops degrade to `api_error` (M4 plan
            # §405-422 row "APIConnectionError within deadline, mid-stream
            # → stay_silent('api_error')") — the router doesn't catch
            # generic Exception here; we own the degrade.
            return BrainDecision.stay_silent("api_error")
        except (
            anthropic.RateLimitError,
            anthropic.APIStatusError,
        ) as exc:
            log.error(
                "brain.api_error",
                t_ms=t_ms,
                kind=type(exc).__name__,
                status=getattr(exc, "status_code", None),
                mode="streaming",
            )
            return BrainDecision.stay_silent("api_error")

    async def _stream_and_assemble(
        self,
        *,
        kwargs: dict[str, Any],
        t_ms: int,
        utterance_listener: UtteranceListener,
    ) -> BrainDecision:
        """Drive the SDK stream and return an assembled `BrainDecision`.

        Wrapped by `_decide_streaming` in `asyncio.wait_for(_BRAIN_DEADLINE_S)`
        and one outer try/except that maps SDK errors to the streaming-
        path decision matrix. Splitting this into its own coroutine lets
        the timeout-and-error scaffolding stay readable.
        """
        utterance_high_water = 0
        first_delta_logged = False

        async with self._client.messages.stream(**kwargs) as stream:
            async for stream_event in stream:
                # We only react to `input_json` events. Other events
                # (message_start, content_block_start/stop, etc.) just
                # accumulate the SDK's internal snapshot — the helper
                # `accumulate_event` runs ahead of `build_events`, so
                # by the time we see them the snapshot is already updated.
                if getattr(stream_event, "type", None) != "input_json":
                    continue
                snapshot = getattr(stream_event, "snapshot", None)
                if not isinstance(snapshot, dict):
                    continue
                utterance = snapshot.get("utterance")
                if not isinstance(utterance, str):
                    continue
                if len(utterance) < _UTTERANCE_LISTENER_MIN_CHARS:
                    continue
                if len(utterance) <= utterance_high_water:
                    # Snapshot didn't grow this tick (SDK can re-emit on
                    # mid-token deltas). Nothing to push.
                    continue
                delta = utterance[utterance_high_water:]
                utterance_high_water = len(utterance)
                if not first_delta_logged:
                    log.info(
                        "brain.stream.utterance.first_delta",
                        t_ms=t_ms,
                        chars=len(delta),
                    )
                    first_delta_logged = True
                # Listener is awaited inline so back-pressure from the
                # downstream TTS path naturally throttles the stream.
                await utterance_listener(delta)
            final_message = await stream.get_final_message()

        if first_delta_logged:
            log.info(
                "brain.stream.utterance.complete",
                t_ms=t_ms,
                total_chars=utterance_high_water,
            )

        usage = _usage_from_response(getattr(final_message, "usage", None), model=self._model)
        decision = _assemble_decision_from_message(final_message, t_ms=t_ms, usage=usage)

        # M4 R3d — schema-violation tail in the interviewer's voice.
        # When post-stream validation rejects the tool_use input AND the
        # candidate already heard partial audio, push a closing token
        # through the SAME listener so the half-sentence resolves on a
        # complete clause. Without this, the next dispatch may steelman
        # an unrelated point and the candidate hears jarring jumps like
        # "Walk me through capacit— [pause] — How would you partition?".
        # Discriminate the row as `schema_violation_partial_recovery` so
        # the eval harness can filter it; the router treats it the same
        # as `schema_violation` for the consecutive-violations counter.
        if decision.reason == "schema_violation" and utterance_high_water > 0:
            try:
                await utterance_listener(_SCHEMA_VIOLATION_TAIL)
            except Exception:
                log.exception(
                    "brain.stream.schema_violation_tail_failed",
                    t_ms=t_ms,
                )
            decision = BrainDecision.schema_violation(
                decision.raw_input or None,
                usage=usage,
            )
            decision = _replace_decision_reason(decision, "schema_violation_partial_recovery")
            log.info(
                "brain.stream.schema_violation_partial_recovery",
                t_ms=t_ms,
            )

        return decision

    async def aclose(self) -> None:
        """Close the underlying httpx pool. Idempotent."""
        await self._client.close()


def _extract_tool_block(response: Message) -> ToolUseBlock | None:
    """Return the `interview_decision` tool_use block, or None."""
    for block in response.content:
        if isinstance(block, ToolUseBlock) and block.name == INTERVIEW_DECISION_TOOL["name"]:
            return block
    return None


def _replace_decision_reason(decision: BrainDecision, reason: str) -> BrainDecision:
    """Return a copy of ``decision`` with a new ``reason`` discriminator.

    Used for the streaming-path post-recovery cases where the decision
    shape is otherwise unchanged but the reason needs a more specific
    suffix for replay tooling. ``BrainDecision`` is a frozen dataclass,
    so we materialise a new instance rather than mutating in place.
    """
    return BrainDecision(
        decision=decision.decision,
        priority=decision.priority,
        confidence=decision.confidence,
        reasoning=decision.reasoning,
        utterance=decision.utterance,
        can_be_skipped_if_stale=decision.can_be_skipped_if_stale,
        state_updates=dict(decision.state_updates),
        reason=reason,
        usage=decision.usage,
        raw_input=dict(decision.raw_input),
        is_partial=decision.is_partial,
    )


def _assemble_decision_from_message(
    response: Any,
    *,
    t_ms: int,
    usage: BrainUsage,
) -> BrainDecision:
    """Validate a complete `Message` and produce a `BrainDecision`.

    Shared between the blocking and streaming entry points so the only
    difference between them is the SDK call shape — error taxonomy and
    `BrainDecision` output identical otherwise. Runs the same four
    checks, in the same order:

    1. ``stop_reason == "tool_use"`` — model honored ``tool_choice``.
    2. ``tool_block`` exists with the expected name.
    3. ``tool_block.input`` is a dict (not a list/string regression).
    4. XML-spillover recovery, then jsonschema validation, then sanitize.

    The function accepts ``Any`` rather than ``Message`` because the
    streaming path delivers the SDK's ``ParsedMessage`` (a subclass) and
    test fakes pass duck-typed stand-ins; the field accesses below are
    the only contract we depend on.
    """
    if getattr(response, "stop_reason", None) != "tool_use":
        log.warning(
            "brain.unexpected_stop",
            t_ms=t_ms,
            stop_reason=getattr(response, "stop_reason", None),
        )
        return BrainDecision.stay_silent("unexpected_stop_reason", usage=usage)

    tool_block = _extract_tool_block(response)
    if tool_block is None:
        log.warning("brain.missing_tool_block", t_ms=t_ms)
        return BrainDecision.stay_silent("missing_tool_block", usage=usage)

    # SDK types tool_block.input as object; cast to dict for downstream
    # consumption. A non-dict here is a Claude-side regression worth
    # loud-erroring on.
    tool_input = tool_block.input
    if not isinstance(tool_input, dict):
        log.warning(
            "brain.schema_violation",
            t_ms=t_ms,
            reason="tool_input_not_object",
        )
        return BrainDecision.schema_violation(None, usage=usage)

    # Recover Opus's XML-tool-use spillover before validation. Opus 4.x
    # intermittently emits nested-object fields as a single string of
    # `<parameter name="...">value</parameter>` even though the schema
    # asks for an object. Observed in M3 dogfood 2026-04-25 when
    # state_updates arrived as one long XML blob, which previously
    # crashed the entire dispatch and silently dropped a real
    # session_summary update.
    tool_input = _recover_xml_tool_input(tool_input, t_ms=t_ms)

    errors = sorted(_DECISION_VALIDATOR.iter_errors(tool_input), key=lambda e: e.path)
    if errors:
        first = errors[0]
        # Log the JSON pointer to the offending field plus a short
        # snippet of the value. Without these the operator can't tell
        # which sub-key the brain mangled — observed during the M3
        # dogfood when Opus emitted `"\n"` for an object-typed field.
        error_path = "/".join(str(part) for part in first.absolute_path) or "<root>"
        offending = repr(first.instance)
        if len(offending) > 80:
            offending = offending[:80] + "…"
        log.warning(
            "brain.schema_violation",
            t_ms=t_ms,
            first_error=first.message,
            error_path=error_path,
            offending_value=offending,
            error_count=len(errors),
        )
        return BrainDecision.schema_violation(tool_input, usage=usage)

    decision = BrainDecision.from_tool_block(tool_input, usage=usage)
    if decision.reason in ("utterance_too_long", "utterance_has_control_chars"):
        log.warning(
            "brain.utterance_rejected",
            t_ms=t_ms,
            reason=decision.reason,
        )

    log.info(
        "brain.call.end",
        t_ms=t_ms,
        decision=decision.decision,
        priority=decision.priority,
        confidence=decision.confidence,
        reason=decision.reason,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cost_usd=usage.cost_usd,
    )
    return decision


# The only object-typed key on `interview_decision` that Opus intermittently
# emits as XML-tool-use spillover. Recovery is applied only to this key;
# all others pass through untouched.
_OBJECT_TYPED_KEY = "state_updates"

_XML_PARAMETER_RE = re.compile(
    r'<parameter\s+name="(?P<name>[^"]+)">(?P<value>.*?)</parameter>',
    re.DOTALL,
)

# Safety cap: refuse to run the regex on inputs larger than this. Far above
# any realistic state_updates payload (~100-500 bytes in observed traffic).
_MAX_RECOVERY_INPUT_BYTES = 32 * 1024  # 32 KiB


def _recover_xml_state_updates(value: str) -> dict[str, str] | None:
    """Parse Opus's XML-parameter blob back into a ``{name: value}`` dict.

    Returns ``None`` when no ``<parameter>`` tags match (caller leaves the
    original value in place) or when the input exceeds the byte cap (DoS
    resistance).
    """
    if len(value.encode("utf-8")) > _MAX_RECOVERY_INPUT_BYTES:
        return None  # Refuse to scan very large inputs (DoS resistance)
    recovered: dict[str, str] = {}
    for match in _XML_PARAMETER_RE.finditer(value):
        recovered[match.group("name")] = match.group("value").strip()
    return recovered if recovered else None


def _recover_xml_tool_input(tool_input: dict[str, Any], *, t_ms: int) -> dict[str, Any]:
    """Inflate Opus's XML-style tool-use spillover back into a dict.

    When Opus emits ``state_updates`` as the literal string
    ``"\\n<parameter name=\\"foo\\">bar</parameter>"`` the strict
    JSON-schema validator below would otherwise reject the entire
    decision and we'd silently drop a real state update. We attempt
    recovery on that single key and return a new dict so the original
    is never mutated.

    Logs ``brain.tool_input_recovered`` so the operator can grep for
    how often the model fell into this format. Recovery is best-effort:
    if no ``<parameter>`` tags match, the original dict is returned
    unchanged to fail validation as before.
    """
    if not isinstance(tool_input.get(_OBJECT_TYPED_KEY), str):
        return tool_input
    recovered = _recover_xml_state_updates(tool_input[_OBJECT_TYPED_KEY])
    if recovered is None:
        return tool_input
    log.info(
        "brain.tool_input_recovered",
        t_ms=t_ms,
        path=_OBJECT_TYPED_KEY,
        recovered_keys=sorted(recovered),
    )
    return {**tool_input, _OBJECT_TYPED_KEY: recovered}


def _usage_from_response(usage: Usage | None, *, model: str) -> BrainUsage:
    """Extract token counts + compute cost from an Anthropic `Usage`.

    `usage` can be None on responses where the SDK omits it; guard
    accordingly rather than assuming the field is always present.
    `cache_creation_input_tokens` and `cache_read_input_tokens` may
    themselves be None even when the top-level `usage` exists — default
    them to 0 so pricing math stays correct.
    """
    if usage is None:
        return BrainUsage()
    input_tokens = usage.input_tokens or 0
    output_tokens = usage.output_tokens or 0
    cache_creation = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    cost_usd = estimate_cost_usd(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    return BrainUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        cost_usd=cost_usd,
    )


_CLIENT_SINGLETON: BrainClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_brain_client(settings: Settings | None = None) -> BrainClient:
    """Return the process-wide BrainClient singleton.

    Mirrors `state/redis_store.get_redis_store` and `audio/stt._load_model`
    — threading.Lock double-check so the default thread-pool executor
    doesn't race two `AsyncAnthropic` constructions on the same process.
    """
    global _CLIENT_SINGLETON
    if _CLIENT_SINGLETON is not None:
        return _CLIENT_SINGLETON
    with _CLIENT_LOCK:
        if _CLIENT_SINGLETON is not None:
            return _CLIENT_SINGLETON
        cfg = settings or get_settings()
        log.info(
            "brain.client.init",
            model=cfg.brain_model,
            base_url=cfg.anthropic_base_url,
        )
        _CLIENT_SINGLETON = BrainClient(cfg, model=cfg.brain_model)
        return _CLIENT_SINGLETON


def reset_brain_client_singleton() -> None:
    """Test-only: drop the cached singleton.

    Tests that construct a `BrainClient` with a mocked transport call
    this so production's singleton doesn't leak across cases.
    """
    global _CLIENT_SINGLETON
    _CLIENT_SINGLETON = None
