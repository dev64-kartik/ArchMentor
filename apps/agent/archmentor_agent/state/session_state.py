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

    def with_state_updates(self, updates: dict[str, Any]) -> SessionState:
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

        An absent or ``None`` value for any sub-key means "no change"
        — this preserves backward-compatibility with partial updates.

        Args:
            updates: The ``state_updates`` dict from ``BrainDecision``.

        Returns:
            A new ``SessionState`` with the updates applied. Returns
            ``self`` unchanged if ``updates`` is empty.
        """
        if not updates:
            return self

        data = self.model_dump()

        phase_advance = updates.get("phase_advance")
        if phase_advance is not None:
            data["phase"] = phase_advance

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

        new_active_argument = updates.get("new_active_argument")
        if new_active_argument is not None:
            data["active_argument"] = new_active_argument

        summary_append = updates.get("session_summary_append")
        if summary_append:
            existing = data.get("session_summary") or ""
            sep = "\n\n" if existing else ""
            data["session_summary"] = f"{existing}{sep}{summary_append}"

        return SessionState.model_validate(data)


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
