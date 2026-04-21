"""Tests for POST /livekit/token."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import archmentor_api.models  # noqa: F401 — register tables on metadata
import jwt
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
def active_session(engine: Engine) -> tuple[UUID, str, str]:
    """Seed an active session owned by a fresh user.

    Returns `(user_id, room, token)` so the test can mint a JWT with the
    correct `sub` and request that specific room.
    """
    user_id = uuid4()
    room = f"session-{uuid4()}"
    with Session(engine) as db:
        problem = Problem(
            slug=f"prob-{uuid4().hex[:8]}",
            title="Test",
            statement_md="# Test",
            difficulty="medium",
            rubric_yaml="dimensions: []",
            ideal_solution_md="...",
            seniority_calibration_json={},
        )
        user = User(id=user_id, email="candidate@example.com")
        db.add(user)
        db.add(problem)
        db.flush()
        row = InterviewSession(
            user_id=user.id,
            problem_id=problem.id,
            problem_version=problem.version,
            status=SessionStatus.ACTIVE,
            livekit_room=room,
            prompt_version="v0",
        )
        db.add(row)
        db.commit()
    return user_id, room, _user_jwt(sub=str(user_id))


@pytest.fixture
def ended_session(engine: Engine) -> tuple[UUID, str, str]:
    user_id = uuid4()
    room = f"session-{uuid4()}"
    with Session(engine) as db:
        problem = Problem(
            slug=f"prob-{uuid4().hex[:8]}",
            title="Test",
            statement_md="# Test",
            difficulty="medium",
            rubric_yaml="dimensions: []",
            ideal_solution_md="...",
            seniority_calibration_json={},
        )
        user = User(id=user_id, email="candidate@example.com")
        db.add(user)
        db.add(problem)
        db.flush()
        row = InterviewSession(
            user_id=user.id,
            problem_id=problem.id,
            problem_version=problem.version,
            status=SessionStatus.ENDED,
            livekit_room=room,
            prompt_version="v0",
        )
        db.add(row)
        db.commit()
    return user_id, room, _user_jwt(sub=str(user_id))


def _user_jwt(
    sub: str = "user-abc",
    *,
    email: str = "candidate@example.com",
    issuer: str | None = None,
    secret: str | None = None,
    expired: bool = False,
) -> str:
    """Mint a Supabase-style access token that `deps.require_user` accepts."""
    now = datetime.now(UTC)
    iat = now - timedelta(minutes=5 if expired else 1)
    exp = now - timedelta(minutes=1) if expired else now + timedelta(minutes=5)
    return jwt.encode(
        {
            "sub": sub,
            "email": email,
            "role": "authenticated",
            "aud": os.environ.get("API_JWT_AUDIENCE", "authenticated"),
            "iss": issuer if issuer is not None else os.environ["API_JWT_ISSUER"],
            "iat": int(iat.timestamp()),
            "exp": int(exp.timestamp()),
        },
        secret if secret is not None else os.environ["API_JWT_SECRET"],
        algorithm="HS256",
    )


def test_mint_token_requires_bearer_auth(
    client: TestClient,
    active_session: tuple[UUID, str, str],
) -> None:
    _, room, _ = active_session
    response = client.post("/livekit/token", json={"room": room})
    assert response.status_code == 401


def test_mint_token_returns_token_url_and_room(
    client: TestClient,
    active_session: tuple[UUID, str, str],
) -> None:
    user_id, room, token = active_session
    response = client.post(
        "/livekit/token",
        json={"room": room},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["room"] == room
    assert body["identity"] == str(user_id)
    assert body["url"].startswith(("ws://", "wss://"))
    assert isinstance(body["token"], str)
    assert len(body["token"]) > 20


def test_mint_token_embeds_correct_identity_and_room(
    client: TestClient,
    active_session: tuple[UUID, str, str],
) -> None:
    """The issued LiveKit JWT must bind identity to user_id and scope to the requested room."""
    user_id, room, token = active_session
    response = client.post(
        "/livekit/token",
        json={"room": room},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    lk_token = response.json()["token"]
    claims = jwt.decode(
        lk_token,
        os.environ["API_LIVEKIT_API_SECRET"],
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert claims["sub"] == str(user_id)
    assert claims["iss"] == os.environ["API_LIVEKIT_API_KEY"]
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["room"] == room
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is True


def test_mint_token_rejects_empty_room(client: TestClient) -> None:
    response = client.post(
        "/livekit/token",
        json={"room": ""},
        headers={"Authorization": f"Bearer {_user_jwt()}"},
    )
    assert response.status_code == 422


def test_mint_token_404s_for_unknown_room(client: TestClient) -> None:
    """Requesting a room with no matching session row must 404, not mint a token."""
    response = client.post(
        "/livekit/token",
        json={"room": "session-does-not-exist"},
        headers={"Authorization": f"Bearer {_user_jwt(sub=str(uuid4()))}"},
    )
    assert response.status_code == 404


def test_mint_token_403s_cross_tenant(
    client: TestClient,
    active_session: tuple[UUID, str, str],
) -> None:
    """Authenticated user A cannot mint a token for user B's session room."""
    _, room, _ = active_session
    attacker_token = _user_jwt(sub=str(uuid4()))
    response = client.post(
        "/livekit/token",
        json={"room": room},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert response.status_code == 403


def test_mint_token_409s_for_ended_session(
    client: TestClient,
    ended_session: tuple[UUID, str, str],
) -> None:
    """An ended/errored session must not accept new room tokens."""
    _, room, token = ended_session
    response = client.post(
        "/livekit/token",
        json={"room": room},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409


def test_mint_token_rejects_expired_user_jwt(
    client: TestClient,
    active_session: tuple[UUID, str, str],
) -> None:
    """An access token past its `exp` claim must not produce a LiveKit token."""
    user_id, room, _ = active_session
    token = _user_jwt(sub=str(user_id), expired=True)
    response = client.post(
        "/livekit/token",
        json={"room": room},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_mint_token_rejects_wrong_signing_secret(
    client: TestClient,
    active_session: tuple[UUID, str, str],
) -> None:
    """A token signed with an unknown secret must fail signature verification."""
    user_id, room, _ = active_session
    token = _user_jwt(
        sub=str(user_id),
        secret="attacker_secret_attacker_secret_attacker_secret_attacker_secret",  # noqa: S106 — fixture value
    )
    response = client.post(
        "/livekit/token",
        json={"room": room},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_mint_token_rejects_wrong_issuer(
    client: TestClient,
    active_session: tuple[UUID, str, str],
) -> None:
    """A token with the wrong `iss` claim must be refused.

    CLAUDE.md flags this as a silent-failure class: if `API_JWT_ISSUER`
    drifts from `GOTRUE_API_EXTERNAL_URL`, every `/me`-style call would
    silently 401. Enforce it end-to-end by verifying the inverse here.
    """
    user_id, room, _ = active_session
    token = _user_jwt(sub=str(user_id), issuer="http://attacker.example.com")
    response = client.post(
        "/livekit/token",
        json={"room": room},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
