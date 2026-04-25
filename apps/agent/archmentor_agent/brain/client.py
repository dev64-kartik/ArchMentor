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
import threading
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

_MAX_TOKENS = 1024

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
    ) -> BrainDecision:
        """Run one brain call and return a `BrainDecision`.

        Never raises on Anthropic 5xx / rate-limit / transport errors
        (after SDK retries) — those degrade to `stay_silent` so the
        voice loop keeps running. 4xx auth/bad-request DO raise:
        they're bugs the operator needs to see.

        `asyncio.CancelledError` propagates. The router relies on it
        to abort the call when the candidate starts speaking.
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
        )

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
        except (
            anthropic.RateLimitError,
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
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

        if response.stop_reason != "tool_use":
            log.warning(
                "brain.unexpected_stop",
                t_ms=t_ms,
                stop_reason=response.stop_reason,
            )
            return BrainDecision.stay_silent("unexpected_stop_reason", usage=usage)

        tool_block = _extract_tool_block(response)
        if tool_block is None:
            log.warning("brain.missing_tool_block", t_ms=t_ms)
            return BrainDecision.stay_silent("missing_tool_block", usage=usage)

        # SDK types tool_block.input as object; cast to dict for
        # downstream consumption. A non-dict here is a Claude-side
        # regression worth loud-erroring on.
        tool_input = tool_block.input
        if not isinstance(tool_input, dict):
            log.warning(
                "brain.schema_violation",
                t_ms=t_ms,
                reason="tool_input_not_object",
            )
            return BrainDecision.schema_violation(None, usage=usage)

        errors = sorted(_DECISION_VALIDATOR.iter_errors(tool_input), key=lambda e: e.path)
        if errors:
            log.warning(
                "brain.schema_violation",
                t_ms=t_ms,
                first_error=errors[0].message,
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

    async def aclose(self) -> None:
        """Close the underlying httpx pool. Idempotent."""
        await self._client.close()


def _extract_tool_block(response: Message) -> ToolUseBlock | None:
    """Return the `interview_decision` tool_use block, or None."""
    for block in response.content:
        if isinstance(block, ToolUseBlock) and block.name == INTERVIEW_DECISION_TOOL["name"]:
            return block
    return None


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
