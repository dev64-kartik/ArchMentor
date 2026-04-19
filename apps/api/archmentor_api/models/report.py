"""Post-session feedback reports."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import jsonb_column, pk_uuid, utcnow


class ReportStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class Report(SQLModel, table=True):
    __tablename__ = "reports"

    id: UUID = Field(default_factory=pk_uuid, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", unique=True, index=True)
    status: ReportStatus = Field(default=ReportStatus.PENDING, nullable=False)

    summary_md: str | None = Field(default=None)
    per_dimension_json: dict[str, object] | None = Field(
        default=None,
        sa_column=jsonb_column(nullable=True),
    )
    strengths: list[str] = Field(default_factory=list, sa_column=jsonb_column())
    gaps: list[str] = Field(default_factory=list, sa_column=jsonb_column())
    next_steps: list[str] = Field(default_factory=list, sa_column=jsonb_column())

    generated_at: datetime | None = Field(default=None)
    model_version: str | None = Field(default=None, max_length=50)

    created_at: datetime = Field(default_factory=utcnow, nullable=False)
