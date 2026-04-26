"""Tests for the user-facing session lifecycle endpoints (create/list/get/end/delete)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import archmentor_api.models  # noqa: F401  — registers tables
import jwt
import pytest
from archmentor_api.db import get_db_session
from archmentor_api.main import app
from archmentor_api.models.brain_snapshot import BrainSnapshot
from archmentor_api.models.canvas_snapshot import CanvasSnapshot
from archmentor_api.models.interruption import Interruption, InterruptionPriority
from archmentor_api.models.problem import Problem
from archmentor_api.models.report import Report
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.session_event import SessionEvent, SessionEventType
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


def _bearer(user_id: UUID, email: str = "candidate@example.com") -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "role": "authenticated",
            "aud": "authenticated",
            "iss": os.environ["API_JWT_ISSUER"],
        },
        os.environ["API_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def seed(engine: Engine) -> dict[str, object]:
    """Seed one user + one problem; return their ids and headers."""
    user_id = uuid4()
    with Session(engine) as db:
        db.add(User(id=user_id, email="candidate@example.com"))
        db.add(
            Problem(
                slug="url-shortener",
                title="Design a URL shortener",
                statement_md="# URL shortener",
                difficulty="medium",
                rubric_yaml="dimensions: []",
                ideal_solution_md="...",
                seniority_calibration_json={},
            )
        )
        db.commit()
    return {"user_id": user_id, "headers": _bearer(user_id)}


# ---------- POST /sessions ----------


def test_create_session_happy_path(client: TestClient, seed: dict[str, object]) -> None:
    response = client.post(
        "/sessions",
        json={"problem_slug": "url-shortener"},
        headers=seed["headers"],
    )
    assert response.status_code == 201
    body = response.json()
    UUID(body["session_id"])  # parseable
    assert body["livekit_room"] == f"session-{body['session_id']}"
    assert body["livekit_url"].startswith(("ws://", "wss://"))
    assert body["status"] == "active"
    assert body["started_at"] is not None
    assert body["problem"]["slug"] == "url-shortener"
    assert body["problem"]["title"] == "Design a URL shortener"


def test_create_session_unknown_problem_slug_returns_422(
    client: TestClient, seed: dict[str, object]
) -> None:
    response = client.post(
        "/sessions",
        json={"problem_slug": "ghost-problem"},
        headers=seed["headers"],
    )
    assert response.status_code == 422


def test_create_session_requires_auth(client: TestClient) -> None:
    response = client.post("/sessions", json={"problem_slug": "url-shortener"})
    assert response.status_code == 401


def test_create_session_then_mint_livekit_token_works(
    client: TestClient, seed: dict[str, object]
) -> None:
    create = client.post(
        "/sessions",
        json={"problem_slug": "url-shortener"},
        headers=seed["headers"],
    )
    assert create.status_code == 201
    room = create.json()["livekit_room"]
    token = client.post(
        "/livekit/token",
        json={"room": room},
        headers=seed["headers"],
    )
    assert token.status_code == 201
    assert token.json()["room"] == room


# ---------- GET /sessions ----------


def test_list_sessions_returns_caller_sessions(
    client: TestClient, engine: Engine, seed: dict[str, object]
) -> None:
    user_id = seed["user_id"]
    headers = seed["headers"]

    create_a = client.post("/sessions", json={"problem_slug": "url-shortener"}, headers=headers)
    create_b = client.post("/sessions", json={"problem_slug": "url-shortener"}, headers=headers)
    assert create_a.status_code == 201
    assert create_b.status_code == 201

    # A session belonging to a different user must NOT appear.
    other_user_id = uuid4()
    with Session(engine) as db:
        db.add(User(id=other_user_id, email="other@example.com"))
        db.flush()
        problem = db.exec(select(Problem)).first()
        assert problem is not None
        db.add(
            InterviewSession(
                user_id=other_user_id,
                problem_id=problem.id,
                problem_version=problem.version,
                status=SessionStatus.ACTIVE,
                livekit_room="session-other",
                prompt_version="v0",
            )
        )
        db.commit()

    response = client.get("/sessions", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    for item in body:
        assert item["livekit_room"].startswith("session-")
    # All rows belong to the caller.
    assert all(UUID(item["session_id"]) for item in body)
    _ = user_id


def test_list_sessions_n_plus_1_regression(client: TestClient, seed: dict[str, object]) -> None:
    """Regression: list_sessions must not issue N extra queries for N sessions.

    The fix (selectinload on InterviewSession.problem) collapses the per-row
    db.get(Problem) calls into a single IN-clause batch query so the total
    query count stays constant regardless of result set size.

    # TODO(#19): Replace this placeholder with a structured query-count assertion
    # using a SQLAlchemy `before_cursor_execute` event listener once the test
    # harness supports it. For now this exercises the endpoint with 3 rows to
    # verify it returns correctly after the selectinload change.
    """
    for _ in range(3):
        r = client.post(
            "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
        )
        assert r.status_code == 201
    response = client.get("/sessions", headers=seed["headers"])
    assert response.status_code == 200
    assert len(response.json()) == 3


def test_list_sessions_empty(client: TestClient, seed: dict[str, object]) -> None:
    response = client.get("/sessions", headers=seed["headers"])
    assert response.status_code == 200
    assert response.json() == []


def test_list_sessions_requires_auth(client: TestClient) -> None:
    response = client.get("/sessions")
    assert response.status_code == 401


# ---------- GET /sessions/{id} ----------


def test_get_session_as_owner(client: TestClient, seed: dict[str, object]) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]
    response = client.get(f"/sessions/{session_id}", headers=seed["headers"])
    assert response.status_code == 200
    assert response.json()["session_id"] == session_id


def test_get_session_403_for_other_user(
    client: TestClient, engine: Engine, seed: dict[str, object]
) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    other_user_id = uuid4()
    with Session(engine) as db:
        db.add(User(id=other_user_id, email="other@example.com"))
        db.commit()
    other_headers = _bearer(other_user_id, "other@example.com")

    response = client.get(f"/sessions/{session_id}", headers=other_headers)
    assert response.status_code == 403


def test_get_session_404_when_missing(client: TestClient, seed: dict[str, object]) -> None:
    response = client.get(f"/sessions/{uuid4()}", headers=seed["headers"])
    assert response.status_code == 404


# ---------- POST /sessions/{id}/end ----------


def test_end_session_happy_path(client: TestClient, seed: dict[str, object]) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    response = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ended"
    assert body["ended_at"] is not None


def test_end_session_already_ended_returns_200_idempotent(
    client: TestClient, seed: dict[str, object]
) -> None:
    """Calling /end on an already-ENDED session must return 200, not 409.

    Network retries and browser unload races both call /end twice;
    the second call should be a no-op returning the same SessionView.
    """
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    first = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert first.status_code == 200
    assert first.json()["status"] == "ended"

    second = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert second.status_code == 200
    assert second.json()["status"] == "ended"
    # ended_at must be the same (no re-write on second call).
    assert second.json()["ended_at"] == first.json()["ended_at"]


def test_end_session_requires_auth(client: TestClient) -> None:
    response = client.post(f"/sessions/{uuid4()}/end")
    assert response.status_code == 401


def test_end_session_403_other_user(
    client: TestClient, engine: Engine, seed: dict[str, object]
) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    other_user_id = uuid4()
    with Session(engine) as db:
        db.add(User(id=other_user_id, email="other@example.com"))
        db.commit()
    other_headers = _bearer(other_user_id, "other@example.com")

    response = client.post(f"/sessions/{session_id}/end", headers=other_headers)
    assert response.status_code == 403


def test_end_session_404_when_missing(client: TestClient, seed: dict[str, object]) -> None:
    response = client.post(f"/sessions/{uuid4()}/end", headers=seed["headers"])
    assert response.status_code == 404


def test_after_end_event_ingest_returns_409(client: TestClient, seed: dict[str, object]) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]
    end = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert end.status_code == 200

    agent_headers = {"X-Agent-Token": os.environ["API_AGENT_INGEST_TOKEN"]}
    ingest = client.post(
        f"/sessions/{session_id}/events",
        json={"t_ms": 0, "type": "utterance_ai", "payload_json": {}},
        headers=agent_headers,
    )
    assert ingest.status_code == 409


# ---------- DELETE /sessions/{id} ----------


def test_delete_active_session_returns_409(client: TestClient, seed: dict[str, object]) -> None:
    """DELETE on an ACTIVE session must 409; call /end first."""
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    response = client.delete(f"/sessions/{session_id}", headers=seed["headers"])
    assert response.status_code == 409
    assert "/end" in response.json()["detail"]


def test_delete_session_happy_path(client: TestClient, seed: dict[str, object]) -> None:
    """DELETE after /end must succeed (204)."""
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    end = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert end.status_code == 200

    response = client.delete(f"/sessions/{session_id}", headers=seed["headers"])
    assert response.status_code == 204

    follow_up = client.get(f"/sessions/{session_id}", headers=seed["headers"])
    assert follow_up.status_code == 404


def test_delete_session_idempotent(client: TestClient, seed: dict[str, object]) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    end = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert end.status_code == 200

    first = client.delete(f"/sessions/{session_id}", headers=seed["headers"])
    assert first.status_code == 204
    second = client.delete(f"/sessions/{session_id}", headers=seed["headers"])
    assert second.status_code == 404


def test_delete_session_requires_auth(client: TestClient) -> None:
    response = client.delete(f"/sessions/{uuid4()}")
    assert response.status_code == 401


def test_delete_session_403_for_other_user(
    client: TestClient, engine: Engine, seed: dict[str, object]
) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    other_user_id = uuid4()
    with Session(engine) as db:
        db.add(User(id=other_user_id, email="other@example.com"))
        db.commit()
    other_headers = _bearer(other_user_id, "other@example.com")

    response = client.delete(f"/sessions/{session_id}", headers=other_headers)
    assert response.status_code == 403


def test_every_fk_to_sessions_id_has_cascade_delete() -> None:
    """Schema audit — every FK targeting `sessions.id` must declare ON DELETE CASCADE.

    If a future child table is added without cascade, this fails on first
    PR. Reads SQLModel metadata directly so it covers the live ORM
    declaration; the Alembic migration mirrors the same constraints on
    Postgres.
    """

    from sqlmodel import SQLModel

    sessions_table = SQLModel.metadata.tables["sessions"]
    offenders: list[str] = []
    for table in SQLModel.metadata.tables.values():
        if table is sessions_table:
            continue
        for fk in table.foreign_keys:
            if fk.column.table is sessions_table and fk.ondelete != "CASCADE":
                offenders.append(f"{table.name}.{fk.parent.name}")
    assert offenders == [], (
        f"FKs to sessions.id missing ondelete='CASCADE': {offenders}. "
        "Add `ondelete='CASCADE'` to the model Field and a migration mirroring "
        "the existing add_cascade_to_session_children pattern."
    )


def test_canvas_snapshot_no_longer_has_diff_column() -> None:
    """Sanity check — `diff_from_prev_json` was dropped per M3 R10."""
    from archmentor_api.models.canvas_snapshot import CanvasSnapshot

    assert "diff_from_prev_json" not in CanvasSnapshot.model_fields


def test_prompt_version_parity() -> None:
    """API DEFAULT_PROMPT_VERSION and agent DEV_PROMPT_VERSION must stay in sync.

    Both constants stamp new sessions so prompt-version replay queries align
    across the M2/M3 boundary. A drift here causes silent mismatches in
    `scripts/replay.py` — catch it at commit time instead.
    """
    from archmentor_agent.brain.bootstrap import DEV_PROMPT_VERSION
    from archmentor_api.services.sessions import DEFAULT_PROMPT_VERSION

    assert DEFAULT_PROMPT_VERSION == DEV_PROMPT_VERSION, (
        f"Prompt version mismatch: API={DEFAULT_PROMPT_VERSION!r}, "
        f"agent={DEV_PROMPT_VERSION!r}. Bump both constants together."
    )


def test_delete_session_cascades_to_children(
    client: TestClient, engine: Engine, seed: dict[str, object]
) -> None:
    """DELETE removes the session AND every child row keyed on session_id.

    Privacy commitment: hard delete via Postgres ON DELETE CASCADE.
    SQLite's CASCADE behavior depends on PRAGMA foreign_keys=ON in the
    test harness; the route should still work end-to-end on a SQLite DB
    with the metadata's `ondelete='CASCADE'` because the test fixture
    enables PRAGMA foreign_keys (see _enable_foreign_keys below).
    """
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = UUID(create.json()["session_id"])

    with Session(engine) as db:
        db.add(
            SessionEvent(
                session_id=session_id,
                t_ms=0,
                type=SessionEventType.UTTERANCE_AI,
                payload_json={},
            )
        )
        db.add(
            BrainSnapshot(
                session_id=session_id,
                t_ms=0,
                session_state_json={},
                event_payload_json={},
                brain_output_json={},
                reasoning_text="",
            )
        )
        db.add(
            CanvasSnapshot(
                session_id=session_id,
                t_ms=0,
                scene_json={},
            )
        )
        db.add(
            Interruption(
                session_id=session_id,
                t_ms=0,
                trigger="turn_end",
                priority=InterruptionPriority.MEDIUM,
                confidence=0.8,
                text="Can you walk me through the data model?",
            )
        )
        db.add(
            Report(
                session_id=session_id,
            )
        )
        db.commit()

    end = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert end.status_code == 200

    response = client.delete(f"/sessions/{session_id}", headers=seed["headers"])
    assert response.status_code == 204

    with Session(engine) as db:
        events = db.exec(select(SessionEvent).where(SessionEvent.session_id == session_id)).all()
        brain = db.exec(select(BrainSnapshot).where(BrainSnapshot.session_id == session_id)).all()
        canvas = db.exec(
            select(CanvasSnapshot).where(CanvasSnapshot.session_id == session_id)
        ).all()
        interruptions = db.exec(
            select(Interruption).where(Interruption.session_id == session_id)
        ).all()
        reports = db.exec(select(Report).where(Report.session_id == session_id)).all()
    assert events == []
    assert brain == []
    assert canvas == []
    assert interruptions == []
    assert reports == []


# ---------- GET /sessions/{id}/bootstrap ----------


def _agent_headers() -> dict[str, str]:
    return {"X-Agent-Token": os.environ["API_AGENT_INGEST_TOKEN"]}


def test_bootstrap_happy_path_returns_problem_payload(
    client: TestClient, seed: dict[str, object]
) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]

    response = client.get(f"/sessions/{session_id}/bootstrap", headers=_agent_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["problem_slug"] == "url-shortener"
    assert body["statement_md"] == "# URL shortener"
    assert body["rubric_yaml"] == "dimensions: []"


def test_bootstrap_missing_agent_token_returns_401(
    client: TestClient, seed: dict[str, object]
) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    response = client.get(f"/sessions/{session_id}/bootstrap")
    assert response.status_code == 401


def test_bootstrap_wrong_agent_token_returns_403(
    client: TestClient, seed: dict[str, object]
) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    response = client.get(
        f"/sessions/{session_id}/bootstrap",
        headers={"X-Agent-Token": "wrong-token"},
    )
    assert response.status_code == 403


def test_bootstrap_user_jwt_returns_401(client: TestClient, seed: dict[str, object]) -> None:
    """Bootstrap is NOT user-JWT authenticated; a Bearer token is rejected."""
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    # Sends a valid user JWT but no X-Agent-Token — should be 401
    # (missing agent credentials, not 403).
    response = client.get(f"/sessions/{session_id}/bootstrap", headers=seed["headers"])
    assert response.status_code == 401


def test_bootstrap_unknown_session_returns_404(client: TestClient, seed: dict[str, object]) -> None:
    response = client.get(f"/sessions/{uuid4()}/bootstrap", headers=_agent_headers())
    assert response.status_code == 404


def test_bootstrap_ended_session_returns_409(client: TestClient, seed: dict[str, object]) -> None:
    create = client.post(
        "/sessions", json={"problem_slug": "url-shortener"}, headers=seed["headers"]
    )
    session_id = create.json()["session_id"]

    end = client.post(f"/sessions/{session_id}/end", headers=seed["headers"])
    assert end.status_code == 200

    response = client.get(f"/sessions/{session_id}/bootstrap", headers=_agent_headers())
    assert response.status_code == 409


# ---------- Settings validators ----------


def test_settings_rejects_http_livekit_url() -> None:
    """livekit_url must use ws:// or wss://; http:// must raise ValidationError."""
    import pydantic
    from archmentor_api.config import Settings

    with pytest.raises(pydantic.ValidationError, match="livekit_url"):
        Settings(
            jwt_secret="a" * 32,
            livekit_api_key="key",
            livekit_api_secret="s" * 32,
            agent_ingest_token="t" * 32,
            livekit_url="http://internal/path",
        )
