"""Problem catalog entries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import jsonb_column, pk_uuid, utcnow


class Problem(SQLModel, table=True):
    __tablename__ = "problems"

    id: UUID = Field(default_factory=pk_uuid, primary_key=True)
    slug: str = Field(max_length=100, index=True)
    version: int = Field(default=1, nullable=False)
    title: str = Field(max_length=200, nullable=False)
    statement_md: str = Field(nullable=False)
    difficulty: str = Field(max_length=20, nullable=False)  # easy | medium | hard

    rubric_yaml: str = Field(nullable=False)
    ideal_solution_md: str = Field(nullable=False)
    seniority_calibration_json: dict[str, object] = Field(sa_column=jsonb_column())

    created_at: datetime = Field(default_factory=utcnow, nullable=False)
