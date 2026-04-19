"""Full SessionState + brain I/O, serialized at every decision point.

Used by `scripts/replay.py` to re-run a historical decision through a
current prompt and diff the output (ghost diff).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import jsonb_column, pk_uuid, utcnow


class BrainSnapshot(SQLModel, table=True):
    __tablename__ = "brain_snapshots"

    id: UUID = Field(default_factory=pk_uuid, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    t_ms: int = Field(nullable=False, index=True)

    session_state_json: dict[str, object] = Field(sa_column=jsonb_column())
    event_payload_json: dict[str, object] = Field(sa_column=jsonb_column())
    brain_output_json: dict[str, object] = Field(sa_column=jsonb_column())
    reasoning_text: str = Field(default="", nullable=False)

    tokens_input: int = Field(default=0, nullable=False)
    tokens_output: int = Field(default=0, nullable=False)

    created_at: datetime = Field(default_factory=utcnow, nullable=False)
