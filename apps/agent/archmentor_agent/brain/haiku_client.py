"""Non-streaming Haiku client for per-session summary compaction (M4 Unit 5).

Mirrors `BrainClient`'s shape — an `AsyncAnthropic` wrapper with a
process-wide singleton — but produces plain-text summaries rather than
tool-use decisions. The compactor runs as a fire-and-forget asyncio
task on `MentorAgent`; the brain dispatch loop is never blocked by it.

Threshold lives inline (`_SUMMARY_COMPACTION_THRESHOLD = 30`) per
refinements R6: a single inline constant next to the client keeps
the knob next to its only consumer. The model id IS exposed via
`Settings.brain_haiku_model` because operators tuning between the
direct-Anthropic and Unbound gateway paths need it for the same
reason `Settings.brain_model` exists.

Logging vocabulary:

- `agent.summary.compaction.begin` — Haiku call dispatched
- `agent.summary.compaction.end`   — Haiku returned, summary applied
- `agent.summary.compaction.failed` — Haiku call raised
"""

from __future__ import annotations

import threading

import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, Usage

from archmentor_agent.brain.decision import BrainUsage
from archmentor_agent.brain.haiku_prompt import (
    SUMMARY_MAX_CHARS,
    SYSTEM_PROMPT,
    build_user_message,
)
from archmentor_agent.brain.pricing import estimate_cost_usd
from archmentor_agent.config import Settings, get_settings
from archmentor_agent.state.session_state import TranscriptTurn

log = structlog.get_logger(__name__)

# Triggers a compaction when ``len(transcript_window) > THRESHOLD`` AND
# no compactor task is in flight. 30 turns ≈ 2-3 minutes of conversation
# under nominal cadence. Raise if M4 dogfood reveals a slower-paced
# phase (deep_dive routinely sits at 40+ turns of dense back-and-forth);
# lower if Anthropic prompt-cost telemetry shows the rolling window
# dominating per-call cost.
_SUMMARY_COMPACTION_THRESHOLD = 30

# Cap a single Haiku call. Compaction is a non-blocking task, but we
# still bound it so a hung gateway doesn't keep a `_summary_in_flight`
# flag flipped True for the rest of the session — which would suppress
# every subsequent threshold-crossing retry. 60 s leaves headroom for
# Haiku's typical sub-second turn-around plus retry/backoff.
_HAIKU_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

_MAX_TOKENS = 512


class HaikuClient:
    """Wraps `AsyncAnthropic` for the summary-compaction call."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncAnthropic | None = None,
        model: str | None = None,
    ) -> None:
        self._model = model or settings.brain_haiku_model
        # `client` is the test seam — tests pass a pre-built
        # AsyncAnthropic with a mocked transport. Production callers
        # omit it and we build one from settings, mirroring `BrainClient`.
        self._client = client or AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            base_url=settings.anthropic_base_url,
            max_retries=2,
            timeout=_HAIKU_TIMEOUT,
        )

    async def compress(
        self,
        *,
        existing_summary: str,
        dropped_turns: list[TranscriptTurn],
    ) -> tuple[str, BrainUsage]:
        """Call Haiku and return ``(new_summary_text, usage)``.

        On any Anthropic SDK error (5xx, rate-limit, connection, timeout
        across retries) the call is allowed to raise — the caller
        (``MentorAgent._run_compaction``) catches and degrades. Letting
        the exception propagate keeps the contract symmetric with
        `BrainClient` for the brain-side errors that DO degrade
        (`stay_silent`): the compactor isn't part of the voice loop, so
        a failed compaction is a logged miss, not a per-turn fallback.
        """
        user_message = build_user_message(
            existing_summary=existing_summary,
            dropped_turns=dropped_turns,
        )
        response: Message = await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = _extract_text(response)
        truncated = _truncate_summary(text)
        usage = _usage_from_response(response.usage, model=self._model)
        return truncated, usage

    async def aclose(self) -> None:
        """Close the underlying httpx pool. Idempotent."""
        await self._client.close()


def _extract_text(response: Message) -> str:
    """Concatenate text blocks from the response body.

    Haiku's plain-text mode returns a list with a single ``TextBlock``,
    but we tolerate a missing or multi-block shape so a future SDK
    change doesn't blow up the compactor loudly. Empty result is
    legitimate (e.g. Haiku returned a structured block we don't read);
    the caller treats an empty summary as "nothing to append."
    """
    parts: list[str] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "".join(parts).strip()


def _truncate_summary(text: str) -> str:
    """Cap the summary at SUMMARY_MAX_CHARS with a ``…`` suffix.

    The prompt asks for under 800 chars but the model occasionally
    exceeds the soft target. Truncating client-side keeps the rolling
    summary bounded so prompt-token growth stays predictable. Logs at
    INFO so an operator pattern of repeated truncations is visible.
    """
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    log.info("agent.summary.compaction.truncated", original_chars=len(text))
    return text[: SUMMARY_MAX_CHARS - 1] + "…"


def _usage_from_response(usage: Usage | None, *, model: str) -> BrainUsage:
    """Same shape as `BrainClient`'s usage helper.

    Duplicated rather than imported to keep `brain.haiku_client` free
    of a `brain.client` dependency — the two modules will diverge
    further as Haiku gains its own retry/streaming semantics.
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


_CLIENT_SINGLETON: HaikuClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_haiku_client(settings: Settings | None = None) -> HaikuClient:
    """Return the process-wide HaikuClient singleton.

    Mirrors `brain/client.get_brain_client` — threading.Lock double-check
    so the default thread-pool executor doesn't race two
    `AsyncAnthropic` constructions on the same process.
    """
    global _CLIENT_SINGLETON
    if _CLIENT_SINGLETON is not None:
        return _CLIENT_SINGLETON
    with _CLIENT_LOCK:
        if _CLIENT_SINGLETON is not None:
            return _CLIENT_SINGLETON
        cfg = settings or get_settings()
        log.info(
            "haiku.client.init",
            model=cfg.brain_haiku_model,
            base_url=cfg.anthropic_base_url,
        )
        _CLIENT_SINGLETON = HaikuClient(cfg, model=cfg.brain_haiku_model)
        return _CLIENT_SINGLETON


def reset_haiku_client_singleton() -> None:
    """Test-only: drop the cached singleton.

    Tests that construct a `HaikuClient` with a mocked transport call
    this so production's singleton doesn't leak across cases.
    """
    global _CLIENT_SINGLETON
    _CLIENT_SINGLETON = None


__all__ = [
    "_SUMMARY_COMPACTION_THRESHOLD",
    "HaikuClient",
    "get_haiku_client",
    "reset_haiku_client_singleton",
]
