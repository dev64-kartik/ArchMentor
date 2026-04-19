"""Shared column helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column
from sqlalchemy.dialects.postgresql import JSONB


def utcnow() -> datetime:
    return datetime.now(UTC)


def pk_uuid() -> UUID:
    return uuid4()


def jsonb_column(*, nullable: bool = False) -> Column:
    """Return a JSONB column that degrades to plain JSON on SQLite.

    Production runs on Postgres (JSONB). Tests use in-memory SQLite where
    JSONB is unavailable; the variant keeps the same Python dict interface
    without forcing a Postgres dependency for unit tests.
    """
    return Column(JSONB().with_variant(JSON(), "sqlite"), nullable=nullable)


__all__ = ["jsonb_column", "pk_uuid", "utcnow"]
