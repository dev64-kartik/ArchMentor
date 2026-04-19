"""Interview session state."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import jsonb_column, pk_uuid, utcnow


class SessionStatus(StrEnum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    ENDED = "ended"
    ERRORED = "errored"


class InterviewSession(SQLModel, table=True):
    __tablename__ = "sessions"

    id: UUID = Field(default_factory=pk_uuid, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    problem_id: UUID = Field(foreign_key="problems.id", index=True)
    problem_version: int = Field(nullable=False)

    status: SessionStatus = Field(default=SessionStatus.SCHEDULED, nullable=False)
    started_at: datetime | None = Field(default=None, index=True)
    ended_at: datetime | None = Field(default=None)
    duration_s_planned: int = Field(default=2700, nullable=False)  # 45 minutes

    livekit_room: str = Field(max_length=100, nullable=False)
    prompt_version: str = Field(max_length=50, nullable=False)

    cost_cap_usd: float = Field(default=5.0, nullable=False)
    cost_actual_usd: float = Field(default=0.0, nullable=False)
    token_totals_json: dict[str, object] = Field(
        default_factory=dict,
        sa_column=jsonb_column(nullable=False),
    )

    created_at: datetime = Field(default_factory=utcnow, nullable=False)
