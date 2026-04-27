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
from typing import Any, Literal

from archmentor_agent.brain import pricing

# Mirrors the enum values in INTERVIEW_DECISION_TOOL["input_schema"]. A
# type-checker catches a drift here at compile time instead of the
# router hitting a surprise string at runtime.
DecisionKind = Literal["speak", "stay_silent", "update_only"]
PriorityKind = Literal["high", "medium", "low"]

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

    This explicitly catches Unicode category ``Cf`` (format) — that
    bucket contains bidi overrides (U+202A..U+202E, U+2066..U+2069)
    and zero-width joiners used in visual spoofing / bidi-exploit
    payloads. Do NOT widen the exemption list beyond the three
    whitespace characters above without revisiting the injection-
    defence posture in ``brain/prompts/system.md`` ``[Security]``.
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

    decision: DecisionKind
    priority: PriorityKind
    confidence: float
    reasoning: str
    utterance: str | None = None
    can_be_skipped_if_stale: bool = False
    state_updates: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None  # populated for non-happy paths only
    usage: BrainUsage = field(default_factory=BrainUsage)
    raw_input: dict[str, Any] = field(default_factory=dict)
    # True only on the streaming-cancel-mid-stream factory below
    # (`BrainDecision.partial`). All other factories leave it False so
    # M5/M6 replay readers fail loudly on the unknown discriminator
    # combination instead of silently classifying a partial-played row
    # as an abstention. See M4 plan R3c.
    #
    # Field name uses `is_partial` rather than `partial` because
    # frozen+slots dataclasses materialize each field as a member
    # descriptor on the class, which would shadow the `partial(...)`
    # classmethod factory below. Keep the factory's name canonical
    # (matches the plan); rename the predicate.
    is_partial: bool = False

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
    def _silent(
        cls,
        *,
        reason: str,
        confidence: float,
        usage: BrainUsage | None = None,
        raw_input: dict[str, Any] | None = None,
    ) -> BrainDecision:
        """Shared skeleton for every ``decision="stay_silent"`` factory.

        Keeps the three public constructors below — ``schema_violation``,
        ``stay_silent``, ``cost_capped`` — as one-liners so adding a
        field to ``BrainDecision`` is a single edit here, not three.
        """
        return cls(
            decision="stay_silent",
            priority="low",
            confidence=confidence,
            reasoning="",
            utterance=None,
            reason=reason,
            usage=usage or BrainUsage(),
            raw_input=dict(raw_input or {}),
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
        return cls._silent(
            reason="schema_violation",
            confidence=0.0,
            usage=usage,
            raw_input=raw_input,
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
        return cls._silent(reason=reason, confidence=0.0, usage=usage)

    @classmethod
    def cost_capped(cls) -> BrainDecision:
        """Synthesized by the router when the session cost cap is hit.

        Confidence is 1.0 (the router is certain the cap is hit;
        no ambiguity). Still has `reason=cost_capped` so the router
        writes a snapshot + ledger event for operator visibility.
        """
        return cls._silent(reason="cost_capped", confidence=1.0)

    @classmethod
    def skipped_idempotent(cls) -> BrainDecision:
        """Router-side short-circuit when the brain-input fingerprint matches the last call.

        The fingerprint hashes a curated subset of state + event payload
        (transcript_turn_count, decisions_count, phase, active_argument
        topic, fingerprint_payload). When two consecutive dispatches see
        identical inputs and the prior decision was ``stay_silent``, no
        Anthropic call is worth making — re-asking the brain the same
        question yields the same answer and burns budget.

        Snapshot + ``brain_decision`` ledger row still emit so replay /
        cost-throttle observability survives. ``BrainUsage`` is empty;
        the ``cost_usd_total`` line in the next CAS apply doesn't move.
        """
        return cls._silent(reason="skipped_idempotent", confidence=1.0)

    @classmethod
    def partial(
        cls,
        *,
        utterance: str,
        reason: str,
        usage: BrainUsage | None = None,
    ) -> BrainDecision:
        """Snapshot of a streaming dispatch cancelled after partial audio played.

        Constructed when `task.cancel()` arrives mid-stream after the
        brain has emitted at least one ``utterance`` delta and the
        candidate has heard partial audio, but before the SDK reaches
        ``message_stop``. The accumulated utterance string is preserved
        so M5/M6 replay tooling can reconstruct exactly what the
        candidate heard; ``decision`` collapses to ``stay_silent`` so
        downstream consumers without the new ``partial`` discriminator
        still treat it as "no further action this turn." The combination
        ``decision="stay_silent" + reason=<...> + partial=True`` is the
        explicit three-part discriminator. See M4 plan R3c.
        """
        return cls(
            decision="stay_silent",
            priority="low",
            confidence=0.0,
            reasoning="",
            utterance=utterance,
            reason=reason,
            usage=usage or BrainUsage(),
            raw_input={},
            is_partial=True,
        )

    @classmethod
    def skipped_cooldown(cls, *, cooldown_ms: int) -> BrainDecision:
        """Router-side short-circuit during the exponential-backoff window.

        After ``N >= 2`` consecutive ``stay_silent`` outcomes, the router
        sets a cooldown of ``min(60_000, 4_000 * 2 ** (N-1))`` ms during
        which non-TURN_END / non-PHASE_TIMER events are skipped. The
        cooldown clears on any speak decision or any TURN_END event;
        PHASE_TIMER bypasses the cooldown gate (refinements R2 — it
        exists *to break a stuck silence*).

        ``cooldown_ms`` carries the active cooldown duration so the
        snapshot row records "how aggressively was the throttle
        engaged."
        """
        decision = cls._silent(reason="stay_silent_backoff", confidence=1.0)
        # Carry cooldown_ms via raw_input — keeps the dataclass shape
        # stable while exposing the diagnostic value to snapshot rows.
        return cls(
            decision=decision.decision,
            priority=decision.priority,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            utterance=decision.utterance,
            reason=decision.reason,
            usage=decision.usage,
            raw_input={"cooldown_ms": int(cooldown_ms)},
        )


__all__ = [
    "BrainDecision",
    "BrainUsage",
    "DecisionKind",
    "PriorityKind",
    "pricing",
    "sanitize_utterance",
]
