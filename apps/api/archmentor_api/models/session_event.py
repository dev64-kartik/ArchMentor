"""Append-only session event ledger.

Every observable event during a session lands here with `{session_id,
t_ms, type, payload_json}`. This is the foundation for replay, eval
harness, and all analytics. Writes are insert-only; never mutate an
existing row.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import jsonb_column, pk_uuid, str_enum_column, utcnow


class SessionEventType(StrEnum):
    UTTERANCE_CANDIDATE = "utterance_candidate"
    UTTERANCE_AI = "utterance_ai"
    BRAIN_DECISION = "brain_decision"
    CANVAS_CHANGE = "canvas_change"
    PHASE_TRANSITION = "phase_transition"
    RUBRIC_UPDATE = "rubric_update"
    DESIGN_DECISION = "design_decision"
    SILENCE_CHECK = "silence_check"
    INTERRUPTION = "interruption"
    ERROR = "error"


class SessionEvent(SQLModel, table=True):
    __tablename__ = "session_events"

    id: UUID = Field(default_factory=pk_uuid, primary_key=True)
    # Composite index (session_id, t_ms) lives in the migration — see session.py
    # for the rationale on why single-column index=True is omitted here.
    session_id: UUID = Field(foreign_key="sessions.id", ondelete="CASCADE")
    t_ms: int = Field(nullable=False)
    type: SessionEventType = Field(sa_column=str_enum_column(SessionEventType, nullable=False))
    payload_json: dict[str, object] = Field(sa_column=jsonb_column())

    created_at: datetime = Field(default_factory=utcnow, nullable=False)
