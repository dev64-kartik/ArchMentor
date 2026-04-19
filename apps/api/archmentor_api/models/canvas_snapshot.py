"""Excalidraw scene snapshots + diffs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import jsonb_column, pk_uuid, utcnow


class CanvasSnapshot(SQLModel, table=True):
    __tablename__ = "canvas_snapshots"

    id: UUID = Field(default_factory=pk_uuid, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)
    t_ms: int = Field(nullable=False, index=True)

    scene_json: dict[str, object] = Field(sa_column=jsonb_column())
    diff_from_prev_json: dict[str, object] | None = Field(
        default=None,
        sa_column=jsonb_column(nullable=True),
    )

    created_at: datetime = Field(default_factory=utcnow, nullable=False)
