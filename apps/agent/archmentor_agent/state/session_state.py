"""SessionState model — the brain's input envelope.

The `decisions` list is NEVER compressed — it lives in full prompt context
for every brain call. Summary compression runs on `transcript_window` only.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class InterviewPhase(StrEnum):
    INTRO = "intro"
    REQUIREMENTS = "requirements"
    CAPACITY = "capacity"
    HLD = "hld"
    DEEP_DIVE = "deep_dive"
    TRADEOFFS = "tradeoffs"
    WRAPUP = "wrapup"


# Soft per-phase budgets (seconds). Sum equals
# ``InterviewSession.duration_s_planned`` (45 min). The phase-timer
# producer (M4 Unit 7) compares ``elapsed_in_phase_s`` to these values
# and dispatches a `PHASE_TIMER` event when over by 50% or more. Inline
# constant rather than a Settings field — there is exactly one consumer
# and the relative phase split is a brain-prompt design choice, not an
# operator knob.
PHASE_SOFT_BUDGETS_S: dict[InterviewPhase, int] = {
    InterviewPhase.INTRO: 120,
    InterviewPhase.REQUIREMENTS: 300,
    InterviewPhase.CAPACITY: 300,
    InterviewPhase.HLD: 780,
    InterviewPhase.DEEP_DIVE: 780,
    InterviewPhase.TRADEOFFS: 300,
    InterviewPhase.WRAPUP: 120,
}

# Minimum gap between PHASE_TIMER dispatches per phase, regardless of
# bucket drift. Backstop to the bucketed-payload + fingerprint-skip
# combo (refinements R5 — "the bucketed payload is the primary cost
# control; the dedup is the safety net").
_PHASE_NUDGE_DEDUP_S = 90


def _bucket_over_budget_pct(over_budget_pct: int) -> int:
    """Coarse tiers ``{50, 100, 200}`` for the PHASE_TIMER event payload.

    Without bucketing, a 30-second tick cadence increments
    ``over_budget_pct`` monotonically and the cost-throttle's
    fingerprint flips on every dispatch — defeating the
    ``skipped_idempotent`` short-circuit during stuck-silence phases
    (the exact case the throttle exists to suppress). With bucketing,
    repeated PHASE_TIMER dispatches inside one tier produce identical
    payload bytes, so the fingerprint matches and the router skips
    them at zero Anthropic cost.
    """
    if over_budget_pct < 100:
        return 50
    if over_budget_pct < 200:
        return 100
    return 200


def _should_nudge(
    *,
    elapsed_in_phase_s: int,
    budget_s: int,
    last_nudge_s: int,
    now_s: int,
) -> bool:
    """Return True iff ``elapsed > budget * 1.5`` AND dedup window passed.

    Dedup is per-phase: the caller maintains a ``{phase: last_nudge_s}``
    mapping and supplies the relevant value. ``budget_s == 0`` is
    treated as "no soft budget configured" → never nudge (defensive
    against a missing `PHASE_SOFT_BUDGETS_S` entry; today every phase
    has one but a future enum value would otherwise silently bypass
    the gate).
    """
    if budget_s <= 0:
        return False
    if elapsed_in_phase_s <= int(budget_s * 1.5):
        return False
    return now_s - last_nudge_s > _PHASE_NUDGE_DEDUP_S


class DesignDecision(BaseModel):
    """Candidate's explicit design decision. Never compressed."""

    t_ms: int
    decision: str  # "Use Kafka for event sourcing"
    reasoning: str  # "Need durability + replay for audit trail"
    alternatives: list[str] = Field(default_factory=list)


class TranscriptTurn(BaseModel):
    t_ms: int
    speaker: str  # "candidate" | "ai"
    text: str


class CoverageStatus(BaseModel):
    covered: bool = False
    depth: str = "none"  # none | shallow | solid | thorough
    last_touched_t_ms: int | None = None


class InterruptionRecord(BaseModel):
    t_ms: int
    trigger: str
    priority: str
    confidence: float
    text: str
    outcome: str | None = None


class CanvasState(BaseModel):
    """In-memory Excalidraw state the brain consumes.

    Distinct from `archmentor_api.models.canvas_snapshot.CanvasSnapshot`,
    which is the durable Postgres row written every few seconds.
    """

    description: str = ""
    last_change_s: int = 0


class PendingUtterance(BaseModel):
    text: str
    generated_at_ms: int
    ttl_ms: int = 10_000


class ActiveArgument(BaseModel):
    topic: str
    opened_at_ms: int
    rounds: int = 0
    candidate_pushed_back: bool = False


class ProblemCard(BaseModel):
    slug: str
    version: int
    title: str
    statement_md: str
    rubric_yaml: str


class SessionState(BaseModel):
    """Full session state handed to the brain on every call."""

    # Static (prompt-cacheable)
    problem: ProblemCard
    system_prompt_version: str

    # Timing
    started_at: datetime
    elapsed_s: int = 0
    remaining_s: int = 2700
    phase: InterviewPhase = InterviewPhase.INTRO
    # Wall-clock seconds (relative to ``started_at``) at which the
    # current ``phase`` was entered. Updated on ``phase_advance`` via
    # ``with_state_updates``. The phase-timer producer uses this as the
    # anchor for ``elapsed_in_phase_s = elapsed_s - last_phase_change_s``
    # so a fresh phase doesn't immediately fire a nudge from a prior
    # phase's overrun.
    last_phase_change_s: int = 0

    # Rolling transcript (verbatim, last 2-3 min)
    transcript_window: list[TranscriptTurn] = Field(default_factory=list)

    # Compressed session history (Haiku-generated every 2-3 min)
    session_summary: str = ""

    # Structured decisions log (NEVER compressed)
    decisions: list[DesignDecision] = Field(default_factory=list)

    # Rubric progress tracker
    rubric_coverage: dict[str, CoverageStatus] = Field(default_factory=dict)

    # Interruption history
    interruptions: list[InterruptionRecord] = Field(default_factory=list)

    # Canvas (structured, not pixels)
    canvas_state: CanvasState = Field(default_factory=CanvasState)

    # Pending utterance awaiting speech-check gate
    pending_utterance: PendingUtterance | None = None

    # Multi-turn counter-argument (interruptible, not fixed 3 rounds)
    active_argument: ActiveArgument | None = None

    # Cost guard
    tokens_input_total: int = 0
    tokens_output_total: int = 0
    cost_usd_total: float = 0.0
    # Per-session cap, seeded from `sessions.cost_cap_usd` at on_enter.
    # The router short-circuits to `BrainDecision.cost_capped()` once
    # `cost_usd_total >= cost_cap_usd`. Default mirrors the API column
    # default ($5 / session).
    cost_cap_usd: float = 5.0

    def with_state_updates(
        self,
        updates: dict[str, Any],
        *,
        key_present_for_active_argument: bool = False,
        now_ms: int | None = None,
    ) -> SessionState:
        """Apply a brain-emitted ``state_updates`` payload.

        The brain's tool schema uses sub-keys that do NOT match
        ``SessionState`` field names 1:1 — ``phase_advance``,
        ``rubric_coverage_delta``, ``new_decision``,
        ``new_active_argument``, ``session_summary_append``. Passing the
        dict through ``model_copy(update=...)`` would silently drop all
        of them because they're not real fields, leaving the decisions
        log empty and phase stuck at ``INTRO`` for the whole session.

        This method performs the explicit translation and re-validates
        the result through Pydantic so a malformed sub-value (e.g.,
        ``phase_advance="bogus"``) raises instead of corrupting state.

        An absent or ``None`` value for the non-``active_argument`` sub-keys
        means "no change" — this preserves backward-compatibility with
        partial updates.

        ``new_active_argument`` is the M4 Unit 8 carve-out: the brain
        distinguishes "object to set/replace" from "explicit ``null`` to
        close" from "key absent to leave unchanged". The schema stays
        ``{type: ["object", "null"]}`` (no string sentinel union), so
        the absent-vs-null distinction is impossible to recover from the
        translated dict alone — the caller must pass
        ``key_present_for_active_argument`` derived from the raw tool
        input. The router's ``_apply_decision`` does exactly this; tests
        that don't care about the FSM may omit the flag (default False
        preserves M2/M3-era replay semantics: no change).

        ``now_ms`` enables the 3-min stale-opener auto-clear branch on
        ``active_argument``. When omitted, auto-clear is skipped — keeps
        replay-determinism for old M2/M3-era snapshots that don't carry
        the timestamp the FSM needs.

        Args:
            updates: The ``state_updates`` dict from ``BrainDecision``.
            key_present_for_active_argument: True iff the brain's raw
                ``state_updates`` carried the ``new_active_argument``
                key (regardless of value). Disambiguates absent-from-
                explicit-null. M4 Unit 8.
            now_ms: Session-relative milliseconds for the stale auto-
                clear branch. None disables auto-clear.

        Returns:
            A new ``SessionState`` with the updates applied. Returns
            ``self`` unchanged if ``updates`` is empty AND no
            active-argument resolution / auto-clear is needed.
        """
        # When the brain emitted nothing we still may need to run the
        # stale auto-clear on a prior open argument; that case requires
        # `now_ms` to be set. Without `now_ms`, a truly empty updates
        # dict is a no-op.
        if not updates and (now_ms is None or self.active_argument is None):
            return self

        data = self.model_dump()

        phase_advance = updates.get("phase_advance")
        if phase_advance is not None:
            data["phase"] = phase_advance
            # M4 Unit 7: refresh ``last_phase_change_s`` so the new
            # phase's timer starts from zero rather than carrying the
            # prior phase's elapsed time. The router passes ``now_ms``
            # (the dispatch's wall-clock anchor); fall back to
            # ``elapsed_s`` for replay/test paths that don't carry
            # ``now_ms``. ``elapsed_s`` is currently a dead field
            # (always 0) — bug tracked separately; the ``now_ms``
            # branch is the production-correct path until then.
            if now_ms is not None:
                data["last_phase_change_s"] = now_ms // 1000
            else:
                data["last_phase_change_s"] = data.get("elapsed_s", 0)

        rubric_delta = updates.get("rubric_coverage_delta")
        if rubric_delta:
            merged = dict(data.get("rubric_coverage") or {})
            for dimension, raw in rubric_delta.items():
                merged[dimension] = _coerce_coverage_status(raw)
            data["rubric_coverage"] = merged

        new_decision = updates.get("new_decision")
        if new_decision:
            decisions = list(data.get("decisions") or [])
            decisions.append(new_decision)
            data["decisions"] = decisions

        # M4 Unit 8: counter-argument FSM. The router computes
        # `key_present` from the raw `tool_input["state_updates"]` dict
        # before the `dict(... or {})` collapse so absent-vs-explicit-null
        # survives. The resolver also runs the 3-min stale auto-clear
        # independently of the brain's emission when ``now_ms`` is set.
        prior_argument = self.active_argument
        new_active_argument_raw = updates.get("new_active_argument")
        resolved_argument = _resolve_active_argument(
            prior=prior_argument,
            new=new_active_argument_raw,
            key_present=key_present_for_active_argument,
            now_ms=now_ms,
        )
        # Serialize the resolved argument back to the dict so Pydantic
        # round-trips it cleanly (model_validate accepts the nested
        # dict, not the dataclass-style object).
        if resolved_argument is None:
            data["active_argument"] = None
        else:
            data["active_argument"] = resolved_argument.model_dump()

        summary_append = updates.get("session_summary_append")
        if summary_append:
            existing = data.get("session_summary") or ""
            sep = "\n\n" if existing else ""
            data["session_summary"] = f"{existing}{sep}{summary_append}"

        return SessionState.model_validate(data)


# M4 Unit 8 — auto-clear an open counter-argument that the brain forgot
# to close when it sat at rounds=0 for longer than this window. Bounds
# the silently-leaked state ``active_argument`` could otherwise carry
# for the rest of the session if the candidate moved on but the brain
# never emitted ``new_active_argument: null``.
_ACTIVE_ARGUMENT_STALE_AUTO_CLEAR_MS = 180_000  # 3 minutes


def _resolve_active_argument(
    *,
    prior: ActiveArgument | None,
    new: dict[str, Any] | None,
    key_present: bool,
    now_ms: int | None,
) -> ActiveArgument | None:
    """Compute the next ``active_argument`` value via the M4 Unit 8 FSM.

    Five transitions on ``key_present + new + prior``:

    1. ``not key_present`` → preserve ``prior`` (brain didn't speak about
       it this turn). The 3-min stale auto-clear may still fire below.
    2. ``key_present and new is None`` → close the argument.
    3. ``key_present and new is dict and prior is None`` → fresh open
       at ``rounds=1, opened_at_ms=now_ms``.
    4. ``key_present and new is dict and prior.topic == new["topic"]``
       → increment ``rounds``, preserve ``opened_at_ms``, take
       ``candidate_pushed_back`` from ``new``.
    5. ``key_present and new is dict and prior.topic != new["topic"]``
       → fresh open with the new topic at ``rounds=1``.

    Auto-clear stale (independent of ``key_present``): if ``prior is not
    None and now_ms - prior.opened_at_ms > _ACTIVE_ARGUMENT_STALE_AUTO_CLEAR_MS
    and prior.rounds == 0``, return None regardless of the FSM result —
    handles the brain forgetting to close an opener it never came back
    to.

    ``now_ms`` is None on replay/legacy paths that don't carry the
    timestamp; in that case the stale auto-clear is skipped and the
    fresh-open / increment branches synthesize a placeholder
    ``opened_at_ms=0`` so the FSM still produces a valid ``ActiveArgument``.
    """
    # Stale auto-clear runs FIRST because the spec says it applies
    # regardless of ``new`` / ``key_present``. A brand-new opener that
    # arrives in the same call would still get applied below if needed.
    # `rounds == 1` is the "brain opened the argument once but never came
    # back to it" state — every fresh-open / topic-change branch below
    # assigns `rounds=1`, so the prior `rounds == 0` condition was
    # permanently unreachable and R18's stale-opener auto-clear never
    # fired. `rounds >= 2` means the brain followed up at least once;
    # those arguments are not stale by definition.
    auto_clear = (
        prior is not None
        and now_ms is not None
        and prior.rounds == 1
        and now_ms - prior.opened_at_ms > _ACTIVE_ARGUMENT_STALE_AUTO_CLEAR_MS
    )
    effective_prior: ActiveArgument | None = None if auto_clear else prior

    if not key_present:
        return effective_prior

    if new is None:
        return None

    if not isinstance(new, dict):
        # Schema is `{type: ["object", "null"]}`; anything else is a
        # validation failure upstream. Defensive: treat as no-change.
        return effective_prior

    # Normalize topic at construction so capitalization / trailing
    # whitespace from Opus (it intermittently varies between
    # `'Caching Strategy'` and `'caching strategy'`) doesn't reset the
    # rounds counter on every turn — without normalization the
    # `rounds >= 3` "let it go" safety valve in the brain prompt never
    # fires for the same semantic topic.
    raw_topic = new.get("topic", "")
    new_topic = _canonical_topic(raw_topic) if isinstance(raw_topic, str) else ""
    candidate_pushed_back = bool(new.get("candidate_pushed_back", False))
    opened_at_ms = now_ms if now_ms is not None else 0

    if effective_prior is None:
        return ActiveArgument(
            topic=new_topic,
            opened_at_ms=opened_at_ms,
            rounds=1,
            candidate_pushed_back=candidate_pushed_back,
        )

    if effective_prior.topic == new_topic:
        return ActiveArgument(
            topic=effective_prior.topic,
            opened_at_ms=effective_prior.opened_at_ms,
            rounds=effective_prior.rounds + 1,
            candidate_pushed_back=candidate_pushed_back,
        )

    # Different topic → fresh open with the new topic.
    return ActiveArgument(
        topic=new_topic,
        opened_at_ms=opened_at_ms,
        rounds=1,
        candidate_pushed_back=candidate_pushed_back,
    )


def _canonical_topic(topic: str) -> str:
    """Normalize a counter-argument topic for FSM equality.

    Applied at construction so the stored ``topic`` is the canonical
    form; comparisons elsewhere can use plain ``==``. Strip + lowercase
    is enough because the brain emits human-readable phrases — we don't
    need stemming or punctuation handling.
    """
    return topic.strip().lower()


_COVERAGE_DEPTHS = ("none", "shallow", "solid", "thorough")


def _coerce_coverage_status(raw: Any) -> dict[str, Any]:
    """Inflate brain shorthand into a `CoverageStatus`-shaped dict.

    Opus reliably emits the bare depth string (e.g. ``"shallow"``) instead
    of the full object even with the schema set, so accept both shapes
    here. Anything else collapses to "covered + depth=shallow" so an
    off-spec emission updates `session_summary_append` rather than
    raising and rolling back the entire dispatch (the M3 dogfood hit
    this on every PG-on-canvas turn).
    """
    if isinstance(raw, str):
        depth = raw if raw in _COVERAGE_DEPTHS else "shallow"
        return {"covered": depth != "none", "depth": depth}
    if isinstance(raw, dict):
        depth = raw.get("depth", "shallow")
        if depth not in _COVERAGE_DEPTHS:
            depth = "shallow"
        return {
            "covered": bool(raw.get("covered", depth != "none")),
            "depth": depth,
            "last_touched_t_ms": raw.get("last_touched_t_ms"),
        }
    return {"covered": True, "depth": "shallow"}
