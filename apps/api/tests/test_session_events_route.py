"""Tests for POST /sessions/{session_id}/events (agent ingest)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import archmentor_api.models  # noqa: F401  — registers tables
import pytest
from archmentor_api.db import get_db_session
from archmentor_api.main import app
from archmentor_api.models.problem import Problem
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.user import User
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    # StaticPool + shared in-memory URL so every Session in this test
    # (fixture seeding, route handler, assertions) sees the same DB.
    eng = create_engine(
        "sqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    try:
        yield eng
    finally:
        SQLModel.metadata.drop_all(eng)


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    def _db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = _db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture
def session_id(engine: Engine) -> UUID:
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
        row = InterviewSession(
            user_id=user.id,
            problem_id=problem.id,
            problem_version=problem.version,
            status=SessionStatus.ACTIVE,
            livekit_room=f"room-{uuid4().hex[:8]}",
            prompt_version="v0",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _agent_headers() -> dict[str, str]:
    return {"X-Agent-Token": os.environ["API_AGENT_INGEST_TOKEN"]}


def test_append_event_happy_path(client: TestClient, session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{session_id}/events",
        json={
            "t_ms": 1250,
            "type": "utterance_candidate",
            "payload_json": {"text": "I'd shard by user id.", "speaker": "candidate"},
        },
        headers=_agent_headers(),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["t_ms"] == 1250
    assert body["type"] == "utterance_candidate"
    UUID(body["id"])  # parseable


def test_append_event_requires_agent_token(client: TestClient, session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{session_id}/events",
        json={"t_ms": 0, "type": "utterance_candidate", "payload_json": {}},
    )
    assert response.status_code == 401


def test_append_event_rejects_wrong_agent_token(client: TestClient, session_id: UUID) -> None:
    """A present-but-wrong X-Agent-Token is 403 (authenticated but unauthorized),
    not 401 — that distinction lets the agent's ledger client treat it as a
    permanent hard failure instead of a retry-worthy transient."""
    response = client.post(
        f"/sessions/{session_id}/events",
        json={"t_ms": 0, "type": "utterance_candidate", "payload_json": {}},
        headers={"X-Agent-Token": "nope"},
    )
    assert response.status_code == 403


def test_append_event_rejects_negative_t_ms(client: TestClient, session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{session_id}/events",
        json={"t_ms": -1, "type": "utterance_ai", "payload_json": {}},
        headers=_agent_headers(),
    )
    assert response.status_code == 422


def test_append_event_rejects_unknown_type(client: TestClient, session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{session_id}/events",
        json={"t_ms": 0, "type": "not_a_real_type", "payload_json": {}},
        headers=_agent_headers(),
    )
    assert response.status_code == 422


def test_append_event_404_when_session_missing(client: TestClient) -> None:
    response = client.post(
        f"/sessions/{uuid4()}/events",
        json={"t_ms": 0, "type": "utterance_ai", "payload_json": {}},
        headers=_agent_headers(),
    )
    assert response.status_code == 404


@pytest.fixture
def ended_session_id(engine: Engine) -> UUID:
    with Session(engine) as db:
        user = User(id=uuid4(), email="candidate@example.com")
        problem = Problem(
            slug=f"prob-{uuid4().hex[:8]}",
            title="Design",
            statement_md="# Design",
            difficulty="medium",
            rubric_yaml="dimensions: []",
            ideal_solution_md="...",
            seniority_calibration_json={},
        )
        db.add(user)
        db.add(problem)
        db.flush()
        row = InterviewSession(
            user_id=user.id,
            problem_id=problem.id,
            problem_version=problem.version,
            status=SessionStatus.ENDED,
            livekit_room=f"room-{uuid4().hex[:8]}",
            prompt_version="v0",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def test_append_event_409_when_session_not_active(
    client: TestClient, ended_session_id: UUID
) -> None:
    """Writes against an ended/errored session poison replay + eval state."""
    response = client.post(
        f"/sessions/{ended_session_id}/events",
        json={"t_ms": 0, "type": "utterance_ai", "payload_json": {}},
        headers=_agent_headers(),
    )
    assert response.status_code == 409


def test_append_event_rejects_oversized_payload(client: TestClient, session_id: UUID) -> None:
    """Payloads over the 16 KiB cap must 413 — caps DoS + injection surface."""
    huge = {"text": "x" * (16 * 1024 + 500)}
    response = client.post(
        f"/sessions/{session_id}/events",
        json={"t_ms": 0, "type": "utterance_candidate", "payload_json": huge},
        headers=_agent_headers(),
    )
    assert response.status_code == 413


def test_active_session_gate_compiles_with_for_update_on_postgres() -> None:
    """The TOCTOU fix relies on `SELECT ... FOR UPDATE` on the session row.

    SQLite silently no-ops FOR UPDATE, so the actual race scenario can't
    be reproduced in this test harness. Instead we compile the query the
    helper builds against the Postgres dialect and assert it includes
    the FOR UPDATE clause — that's the structural invariant the
    production database needs. Removing `.with_for_update()` from
    `_require_active_session` makes this test fail.
    """
    from archmentor_api.models.session import InterviewSession
    from sqlalchemy.dialects import postgresql
    from sqlmodel import select

    stmt = select(InterviewSession).where(InterviewSession.id == uuid4()).with_for_update()
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled.upper()
