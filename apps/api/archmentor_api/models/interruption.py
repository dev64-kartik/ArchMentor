"""Brain interruption records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import pk_uuid, utcnow


class InterruptionPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Interruption(SQLModel, table=True):
    __tablename__ = "interruptions"

    id: UUID = Field(default_factory=pk_uuid, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    t_ms: int = Field(nullable=False, index=True)

    trigger: str = Field(max_length=50, nullable=False)
    priority: InterruptionPriority = Field(nullable=False)
    confidence: float = Field(nullable=False)
    text: str = Field(nullable=False)
    candidate_response_window_ms: int | None = Field(default=None)
    round_number: int = Field(default=1, nullable=False)
    outcome: str | None = Field(default=None, max_length=50)

    created_at: datetime = Field(default_factory=utcnow, nullable=False)
