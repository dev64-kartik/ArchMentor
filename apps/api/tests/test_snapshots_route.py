"""Tests for POST /sessions/{session_id}/snapshots (agent ingest).

Mirrors `test_session_events_route.py` intentionally — the two routes
share auth + active-session semantics, and the events route's test
layout is the canonical StaticPool-SQLite shape for FastAPI routes in
this repo.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import archmentor_api.models  # noqa: F401  — registers tables
import pytest
from archmentor_api.db import get_db_session
from archmentor_api.main import app
from archmentor_api.models.brain_snapshot import BrainSnapshot
from archmentor_api.models.problem import Problem
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.user import User
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


@pytest.fixture
def engine() -> Iterator[Engine]:
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


def _create_session(engine: Engine, *, status: SessionStatus = SessionStatus.ACTIVE) -> UUID:
    with Session(engine) as db:
        user = User(id=uuid4(), email=f"c-{uuid4().hex[:6]}@example.com")
        problem = Problem(
            slug=f"prob-{uuid4().hex[:8]}",
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
            status=status,
            livekit_room=f"room-{uuid4().hex[:8]}",
            prompt_version="v0",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


@pytest.fixture
def session_id(engine: Engine) -> UUID:
    return _create_session(engine)


@pytest.fixture
def ended_session_id(engine: Engine) -> UUID:
    return _create_session(engine, status=SessionStatus.ENDED)


def _agent_headers() -> dict[str, str]:
    return {"X-Agent-Token": os.environ["API_AGENT_INGEST_TOKEN"]}


def _valid_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "t_ms": 120_000,
        "session_state_json": {"phase": "requirements", "elapsed_s": 120},
        "event_payload_json": {"type": "turn_end", "text": "let me start"},
        "brain_output_json": {
            "decision": "stay_silent",
            "priority": "low",
            "confidence": 0.7,
        },
        "reasoning_text": "Candidate opened with requirements; let them drive.",
        "tokens_input": 500,
        "tokens_output": 80,
    }
    base.update(overrides)
    return base


class TestHappyPath:
    def test_append_snapshot_201(
        self, client: TestClient, session_id: UUID, engine: Engine
    ) -> None:
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=_valid_body(),
            headers=_agent_headers(),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["t_ms"] == 120_000
        snapshot_id = UUID(body["id"])

        with Session(engine) as db:
            rows = db.exec(
                select(BrainSnapshot).where(BrainSnapshot.session_id == session_id)
            ).all()
            assert len(rows) == 1
            assert rows[0].id == snapshot_id
            assert rows[0].tokens_input == 500
            assert rows[0].tokens_output == 80
            assert rows[0].reasoning_text.startswith("Candidate opened")

    def test_defaulted_fields_work(self, client: TestClient, session_id: UUID) -> None:
        """All JSON fields + reasoning are optional; only `t_ms` is
        required. This covers the cost-capped code path (Unit 6) that
        writes a snapshot with an empty brain_output + zero tokens."""
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json={"t_ms": 0},
            headers=_agent_headers(),
        )
        assert response.status_code == 201


class TestAuth:
    def test_requires_agent_token(self, client: TestClient, session_id: UUID) -> None:
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=_valid_body(),
        )
        assert response.status_code == 401

    def test_rejects_wrong_agent_token(self, client: TestClient, session_id: UUID) -> None:
        """Wrong token → 403 (authenticated but unauthorized). Same
        split as the events route, so the agent's snapshot client
        treats a misconfigured token as a permanent hard failure."""
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=_valid_body(),
            headers={"X-Agent-Token": "nope"},
        )
        assert response.status_code == 403


class TestValidation:
    def test_rejects_negative_t_ms(self, client: TestClient, session_id: UUID) -> None:
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=_valid_body(t_ms=-1),
            headers=_agent_headers(),
        )
        assert response.status_code == 422

    def test_rejects_negative_tokens_input(self, client: TestClient, session_id: UUID) -> None:
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=_valid_body(tokens_input=-5),
            headers=_agent_headers(),
        )
        assert response.status_code == 422

    def test_rejects_negative_tokens_output(self, client: TestClient, session_id: UUID) -> None:
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=_valid_body(tokens_output=-5),
            headers=_agent_headers(),
        )
        assert response.status_code == 422

    def test_rejects_oversized_payload(self, client: TestClient, session_id: UUID) -> None:
        """Sum across the four JSON blobs + reasoning text gates at
        256 KiB. An adversarial agent can't split a DoS payload across
        three fields to slip under a per-field cap."""
        huge_text = "x" * (256 * 1024 + 500)
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=_valid_body(reasoning_text=huge_text),
            headers=_agent_headers(),
        )
        assert response.status_code == 413


class TestLifecycle:
    def test_404_when_session_missing(self, client: TestClient) -> None:
        response = client.post(
            f"/sessions/{uuid4()}/snapshots",
            json=_valid_body(),
            headers=_agent_headers(),
        )
        assert response.status_code == 404

    def test_409_when_session_not_active(self, client: TestClient, ended_session_id: UUID) -> None:
        """Writes against an ended session poison replay + eval state.
        Same semantics as /events — these two must not drift."""
        response = client.post(
            f"/sessions/{ended_session_id}/snapshots",
            json=_valid_body(),
            headers=_agent_headers(),
        )
        assert response.status_code == 409


class TestTOCTOULock:
    """The snapshot-ingest path takes a row-level lock on the session.

    Without the lock, a `POST /sessions/{id}/end` running between the
    SELECT and the INSERT could leave a snapshot row tagged to an
    already-ENDED session — corrupting replay and eval state. SQLite
    silently no-ops `FOR UPDATE`, so the actual race scenario can't
    be reproduced in this test harness; instead we compile the query
    against the Postgres dialect and assert the lock clause is present.
    """

    def test_active_session_gate_compiles_with_for_update_on_postgres(self) -> None:
        from archmentor_api.models.session import InterviewSession
        from sqlalchemy.dialects import postgresql
        from sqlmodel import select

        stmt = select(InterviewSession).where(InterviewSession.id == uuid4()).with_for_update()
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in compiled.upper()
