"""User reflection of the Supabase Auth row.

The source of truth is GoTrue's `auth.users`. This local table mirrors
what we need for FK relationships; it is populated lazily on first
authed request (or by a sync job later).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Field, SQLModel

from archmentor_api.models._base import utcnow


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: UUID = Field(primary_key=True)  # matches auth.users.id
    email: str | None = Field(default=None, max_length=320, index=True)
    created_at: datetime = Field(default_factory=utcnow, nullable=False)
