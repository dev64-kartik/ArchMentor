"""Shared column helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB


def utcnow() -> datetime:
    return datetime.now(UTC)


def pk_uuid() -> UUID:
    return uuid4()


def jsonb_column(*, nullable: bool = False) -> Column:
    """Return a JSONB column suitable for use as `sa_column=...`."""
    return Column(JSONB, nullable=nullable)


__all__ = ["jsonb_column", "pk_uuid", "utcnow"]
