"""Typed result returned by `BrainClient.decide(...)`.

`BrainDecision` is the one shape the event router consumes. It carries
the brain's structured output (decision/priority/confidence/utterance),
private reasoning (persisted to `brain_snapshots`, never spoken), and
the usage + cost fields the router rolls up into `SessionState` for the
cost guard.

Three factory constructors cover the non-happy paths so the router never
has to construct a fallback decision by hand:

- `from_tool_block` — the Anthropic tool_use block came back well-formed
  and jsonschema-valid; produce the normal decision.
- `schema_violation` — the tool_use block decoded but failed
  `jsonschema.validate`. Emit `stay_silent` with `reason=schema_violation`
  so the router can increment its consecutive-violations counter.
- `stay_silent` — generic safe-fallback (unexpected stop_reason,
  utterance sanitization rejection, Anthropic retriable error after
  SDK retries).
- `cost_capped` — produced by the router (not the client) when
  `state.cost_usd_total >= state.cost_cap_usd`; included here so the
  cost-cap short-circuit still writes a snapshot with a real
  `BrainDecision` object.

Utterance sanitization — max 600 chars, no control chars — lives on
this module rather than in `client.py` because the router calls it
again when it receives a pre-built decision (e.g. `cost_capped`), so
a single sanitizer is the defense-in-depth boundary.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from archmentor_agent.brain import pricing

# Prompt-injection belt: if the model emits a pathologically long
# utterance (e.g. a prompt-injection payload echoing back), drop it.
# 600 chars is ~3-5 spoken sentences, already beyond our "one sentence,
# rarely two" style rule — so a >=600-char reply is a signal, not a
# length-budget issue.
_UTTERANCE_MAX_CHARS = 600


def _utterance_has_control_chars(text: str) -> bool:
    """Return True if any code point is a control category (Cc/Cf/Cs/Co/Cn).

    Newline, carriage return, and tab are allowed — Kokoro tokenizes on
    whitespace, and the brain legitimately emits multi-sentence
    utterances with a newline between them.
    """
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            continue
        if unicodedata.category(ch).startswith("C"):
            return True
    return False


def sanitize_utterance(text: str | None) -> tuple[str | None, str | None]:
    """Return `(sanitized_text, rejection_reason)`.

    The tuple shape makes the caller's job explicit: either we have a
    usable string (and `reason is None`), or we have a reason to drop
    it (and the caller should return a `stay_silent` decision with
    `reason=rejection_reason`).
    """
    if text is None:
        return None, None
    if len(text) > _UTTERANCE_MAX_CHARS:
        return None, "utterance_too_long"
    if _utterance_has_control_chars(text):
        return None, "utterance_has_control_chars"
    return text, None


@dataclass(frozen=True, slots=True)
class BrainUsage:
    """Token counts + computed USD cost for one brain call.

    `input_tokens`, `cache_creation_input_tokens`, and
    `cache_read_input_tokens` are reported separately rather than
    pre-summed so `brain_snapshots` rows can answer "did the cache
    actually kick in on this call?" at replay time.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def tokens_input_total(self) -> int:
        """Sum of all input-side tokens (non-cached + cache-write + cache-read)."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


@dataclass(frozen=True, slots=True)
class BrainDecision:
    """Single brain-call outcome handed to the event router.

    `raw_input` is the tool_use.input dict as returned by the SDK (or
    a synthesized equivalent for fallback cases). Persisted verbatim
    into `brain_snapshots.brain_output_json` for exact replay.
    """

    decision: str  # "speak" | "stay_silent" | "update_only"
    priority: str  # "high" | "medium" | "low"
    confidence: float
    reasoning: str
    utterance: str | None = None
    can_be_skipped_if_stale: bool = False
    state_updates: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None  # populated for non-happy paths only
    usage: BrainUsage = field(default_factory=BrainUsage)
    raw_input: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tool_block(
        cls,
        tool_input: dict[str, Any],
        *,
        usage: BrainUsage,
    ) -> BrainDecision:
        """Build a decision from a jsonschema-valid tool_use.input dict.

        Callers MUST have run `jsonschema.validate` before invoking this
        constructor — the `required` fields are read with `[...]`, not
        `.get(...)`, so a schema-invalid dict will raise KeyError rather
        than silently degrading. This is the contract that makes the
        `schema_violation` path meaningful.

        Utterance sanitization runs here; if the utterance is rejected,
        the decision degrades to `stay_silent` with
        `reason="utterance_rejected"` but the original `raw_input` is
        still persisted for replay.
        """
        utterance, rejection = sanitize_utterance(tool_input.get("utterance"))
        if rejection is not None:
            return cls(
                decision="stay_silent",
                priority="low",
                confidence=float(tool_input["confidence"]),
                reasoning=tool_input["reasoning"],
                utterance=None,
                can_be_skipped_if_stale=bool(tool_input.get("can_be_skipped_if_stale", False)),
                state_updates=dict(tool_input.get("state_updates") or {}),
                reason=rejection,
                usage=usage,
                raw_input=dict(tool_input),
            )

        return cls(
            decision=tool_input["decision"],
            priority=tool_input["priority"],
            confidence=float(tool_input["confidence"]),
            reasoning=tool_input["reasoning"],
            utterance=utterance,
            can_be_skipped_if_stale=bool(tool_input.get("can_be_skipped_if_stale", False)),
            state_updates=dict(tool_input.get("state_updates") or {}),
            usage=usage,
            raw_input=dict(tool_input),
        )

    @classmethod
    def schema_violation(
        cls,
        raw_input: dict[str, Any] | None,
        *,
        usage: BrainUsage | None = None,
    ) -> BrainDecision:
        """Degraded decision when tool_use.input fails jsonschema.validate.

        The router treats `reason=schema_violation` specially — three
        consecutive schema violations trigger the
        `brain.schema_violation.escalated` log + ledger event.
        """
        return cls(
            decision="stay_silent",
            priority="low",
            confidence=0.0,
            reasoning="",
            utterance=None,
            reason="schema_violation",
            usage=usage or BrainUsage(),
            raw_input=dict(raw_input or {}),
        )

    @classmethod
    def stay_silent(
        cls,
        reason: str,
        *,
        usage: BrainUsage | None = None,
    ) -> BrainDecision:
        """Safe default — no speech, no state mutation, logged `reason`.

        Used for unexpected `stop_reason`, post-retry Anthropic failures,
        and any other "voice loop must not break" path.
        """
        return cls(
            decision="stay_silent",
            priority="low",
            confidence=0.0,
            reasoning="",
            utterance=None,
            reason=reason,
            usage=usage or BrainUsage(),
        )

    @classmethod
    def cost_capped(cls) -> BrainDecision:
        """Synthesized by the router when the session cost cap is hit.

        Confidence is 1.0 (the router is certain the cap is hit;
        no ambiguity). Still has `reason=cost_capped` so the router
        writes a snapshot + ledger event for operator visibility.
        """
        return cls(
            decision="stay_silent",
            priority="low",
            confidence=1.0,
            reasoning="",
            utterance=None,
            reason="cost_capped",
            usage=BrainUsage(),
        )


__all__ = [
    "BrainDecision",
    "BrainUsage",
    "pricing",
    "sanitize_utterance",
]
