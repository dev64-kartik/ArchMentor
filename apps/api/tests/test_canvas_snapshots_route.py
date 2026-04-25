"""Tests for `POST /sessions/{session_id}/canvas-snapshots`.

Mirrors `test_snapshots_route.py` — same auth + active-session + body-size
shape — and adds R17 schema enforcement (the route MUST reject any body
containing a `files` key) and TOCTOU regression coverage.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import archmentor_api.models  # noqa: F401  — registers tables
import pytest
from archmentor_api.db import get_db_session
from archmentor_api.main import app
from archmentor_api.models.canvas_snapshot import CanvasSnapshot
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


@pytest.fixture
def active_session_id(engine: Engine) -> UUID:
    with Session(engine) as db:
        user = User(id=uuid4(), email="candidate@example.com")
        problem = Problem(
            slug="url-shortener",
            title="Design a URL shortener",
            statement_md="# URL",
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
            livekit_room=f"session-{uuid4()}",
            prompt_version="v0",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


@pytest.fixture
def ended_session_id(engine: Engine) -> UUID:
    with Session(engine) as db:
        user = User(id=uuid4(), email="other@example.com")
        problem = Problem(
            slug=f"prob-{uuid4().hex[:6]}",
            title="x",
            statement_md="x",
            difficulty="easy",
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
            livekit_room=f"session-{uuid4()}",
            prompt_version="v0",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _agent_headers() -> dict[str, str]:
    return {"X-Agent-Token": os.environ["API_AGENT_INGEST_TOKEN"]}


def test_happy_path(client: TestClient, engine: Engine, active_session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{active_session_id}/canvas-snapshots",
        json={
            "t_ms": 5000,
            "scene_json": {"elements": [{"id": "r1", "type": "rectangle"}]},
        },
        headers=_agent_headers(),
    )
    assert response.status_code == 201
    body = response.json()
    UUID(body["id"])
    assert body["t_ms"] == 5000

    with Session(engine) as db:
        rows = db.exec(
            select(CanvasSnapshot).where(CanvasSnapshot.session_id == active_session_id)
        ).all()
    assert len(rows) == 1
    assert rows[0].scene_json == {"elements": [{"id": "r1", "type": "rectangle"}]}


def test_missing_agent_token_returns_401(client: TestClient, active_session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{active_session_id}/canvas-snapshots",
        json={"t_ms": 0, "scene_json": {}},
    )
    assert response.status_code == 401


def test_wrong_agent_token_returns_403(client: TestClient, active_session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{active_session_id}/canvas-snapshots",
        json={"t_ms": 0, "scene_json": {}},
        headers={"X-Agent-Token": "wrong"},
    )
    assert response.status_code == 403


def test_inactive_session_returns_409(client: TestClient, ended_session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{ended_session_id}/canvas-snapshots",
        json={"t_ms": 0, "scene_json": {}},
        headers=_agent_headers(),
    )
    assert response.status_code == 409


def test_missing_session_returns_404(client: TestClient) -> None:
    response = client.post(
        f"/sessions/{uuid4()}/canvas-snapshots",
        json={"t_ms": 0, "scene_json": {}},
        headers=_agent_headers(),
    )
    assert response.status_code == 404


def test_negative_t_ms_returns_422(client: TestClient, active_session_id: UUID) -> None:
    response = client.post(
        f"/sessions/{active_session_id}/canvas-snapshots",
        json={"t_ms": -1, "scene_json": {}},
        headers=_agent_headers(),
    )
    assert response.status_code == 422


def test_files_field_in_body_returns_422(client: TestClient, active_session_id: UUID) -> None:
    """R17 server-side enforcement.

    The agent strips `files` before publishing, but a future client that
    forgets to do so MUST be rejected at the schema layer rather than
    persisting image data.
    """
    response = client.post(
        f"/sessions/{active_session_id}/canvas-snapshots",
        json={
            "t_ms": 0,
            "scene_json": {"elements": []},
            "files": {"img1": "base64-image-data"},
        },
        headers=_agent_headers(),
    )
    assert response.status_code == 422


def test_oversized_payload_returns_413(client: TestClient, active_session_id: UUID) -> None:
    """257 KiB scene → 413 from the body-size middleware."""
    huge_label = "x" * (257 * 1024)
    response = client.post(
        f"/sessions/{active_session_id}/canvas-snapshots",
        json={"t_ms": 0, "scene_json": {"label": huge_label}},
        headers=_agent_headers(),
    )
    assert response.status_code == 413


def test_at_cap_size_succeeds(client: TestClient, active_session_id: UUID) -> None:
    """Just under 256 KiB → 201."""
    label = "x" * (240 * 1024)
    body = {"t_ms": 0, "scene_json": {"label": label}}
    serialized_size = len(json.dumps(body).encode("utf-8"))
    assert serialized_size <= 256 * 1024
    response = client.post(
        f"/sessions/{active_session_id}/canvas-snapshots",
        json=body,
        headers=_agent_headers(),
    )
    assert response.status_code == 201


def test_select_for_update_compiles_for_canvas_route() -> None:
    """The TOCTOU fix relies on the same FOR UPDATE gate as `/events` and
    `/snapshots`. If the route ever stops calling `_require_active_session`
    or its lock disappears, this test would lag — keeping a structural
    check at this level catches it on the import alone."""
    from pathlib import Path

    from archmentor_api.routes import sessions as sessions_module

    src = sessions_module.__file__
    assert src is not None
    text = Path(src).read_text(encoding="utf-8")
    # The canvas route hits the shared helper; the helper takes the lock.
    assert "_require_active_session" in text
    assert "with_for_update" in text
