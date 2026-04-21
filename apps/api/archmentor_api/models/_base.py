"""Shared column helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, Enum
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


def str_enum_column[E: StrEnum](
    enum_cls: type[E], *, nullable: bool = False, default: E | None = None
) -> Column:
    """Return an Enum column that serializes to Postgres using enum *values*, not names.

    SQLAlchemy's default `Enum` type sends the Python enum member name
    (e.g. `ACTIVE`) to Postgres. Our enum types are `StrEnum`s whose
    values are already the canonical lowercase strings stored in
    Postgres — this helper forces SQLAlchemy to send `.value` via
    `values_callable`.
    """
    return Column(
        Enum(
            enum_cls,
            values_callable=lambda cls: [member.value for member in cls],
        ),
        nullable=nullable,
        default=default,
    )


__all__ = ["jsonb_column", "pk_uuid", "str_enum_column", "utcnow"]
