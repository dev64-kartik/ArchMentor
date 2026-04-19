"""Database engine + session factory."""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session, create_engine

from archmentor_api.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    echo=_settings.debug,
    pool_pre_ping=True,
)


def get_db_session() -> Iterator[Session]:
    """FastAPI dependency yielding a short-lived DB session."""
    with Session(engine) as session:
        yield session
