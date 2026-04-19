"""Write-through integration test for the event ledger.

Uses an in-memory SQLite engine so the test runs without the docker
stack. JSONB columns degrade to plain JSON via `with_variant` in
`archmentor_api.models._base.jsonb_column`.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

# Registers every table on SQLModel.metadata.
import archmentor_api.models  # noqa: F401
import pytest
from archmentor_api.models.problem import Problem
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.session_event import SessionEvent, SessionEventType
from archmentor_api.models.user import User
from archmentor_api.services.event_ledger import append_event
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(eng)
    try:
        yield eng
    finally:
        SQLModel.metadata.drop_all(eng)


@pytest.fixture
def live_session_id(engine: Engine) -> Iterator[str]:
    """Seed a user + problem + session so event_id's FK is satisfiable."""
    with Session(engine) as db:
        user = User(id=uuid4(), email="candidate@example.com")
        problem = Problem(
            slug="url-shortener",
            title="Design a URL shortener",
            statement_md="# Design",
            difficulty="medium",
            rubric_yaml="dimensions: []",
            ideal_solution_md="...",
            seniority_calibration_json={},
        )
        db.add(user)
        db.add(problem)
        db.flush()

        session_row = InterviewSession(
            user_id=user.id,
            problem_id=problem.id,
            problem_version=problem.version,
            status=SessionStatus.ACTIVE,
            livekit_room=f"room-{uuid4().hex[:8]}",
            prompt_version="v0",
        )
        db.add(session_row)
        db.commit()
        db.refresh(session_row)
        yield str(session_row.id)


def test_append_event_persists_row(engine: Engine, live_session_id: str) -> None:
    from uuid import UUID

    session_id = UUID(live_session_id)
    with Session(engine) as db:
        event = append_event(
            db,
            session_id=session_id,
            t_ms=1_250,
            event_type=SessionEventType.UTTERANCE_CANDIDATE,
            payload={"text": "I'd use a hash-based shortener.", "speaker": "candidate"},
        )
        db.commit()

        assert event.id is not None
        assert event.created_at is not None

    # Reopen to prove the row round-trips through the DB, not just the cache.
    with Session(engine) as db:
        stored = db.exec(select(SessionEvent).where(SessionEvent.session_id == session_id)).one()
        assert stored.type is SessionEventType.UTTERANCE_CANDIDATE
        assert stored.t_ms == 1_250
        assert stored.payload_json == {
            "text": "I'd use a hash-based shortener.",
            "speaker": "candidate",
        }


def test_append_event_is_insert_only(engine: Engine, live_session_id: str) -> None:
    from uuid import UUID

    session_id = UUID(live_session_id)
    with Session(engine) as db:
        append_event(
            db,
            session_id=session_id,
            t_ms=0,
            event_type=SessionEventType.PHASE_TRANSITION,
            payload={"to": "requirements"},
        )
        append_event(
            db,
            session_id=session_id,
            t_ms=500,
            event_type=SessionEventType.UTTERANCE_AI,
            payload={"text": "Walk me through requirements."},
        )
        db.commit()

        rows = db.exec(
            select(SessionEvent).where(SessionEvent.session_id == session_id).order_by("t_ms")
        ).all()
        assert [r.t_ms for r in rows] == [0, 500]
        assert [r.type for r in rows] == [
            SessionEventType.PHASE_TRANSITION,
            SessionEventType.UTTERANCE_AI,
        ]


def test_append_event_rejects_negative_t_ms(engine: Engine, live_session_id: str) -> None:
    from uuid import UUID

    session_id = UUID(live_session_id)
    with Session(engine) as db, pytest.raises(ValueError, match="non-negative"):
        append_event(
            db,
            session_id=session_id,
            t_ms=-1,
            event_type=SessionEventType.ERROR,
            payload={},
        )
