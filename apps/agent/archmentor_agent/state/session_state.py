"""SessionState model — the brain's input envelope.

The `decisions` list is NEVER compressed — it lives in full prompt context
for every brain call. Summary compression runs on `transcript_window` only.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

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
