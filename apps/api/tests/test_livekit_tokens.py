"""Tests for POST /livekit/token."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from archmentor_api.main import app
from fastapi.testclient import TestClient


def _user_jwt(sub: str = "user-abc", email: str = "candidate@example.com") -> str:
    """Mint a Supabase-style access token that `deps.require_user` accepts."""
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": sub,
            "email": email,
            "role": "authenticated",
            "aud": os.environ.get("API_JWT_AUDIENCE", "authenticated"),
            "iss": os.environ["API_JWT_ISSUER"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        os.environ["API_JWT_SECRET"],
        algorithm="HS256",
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_mint_token_requires_bearer_auth(client: TestClient) -> None:
    response = client.post("/livekit/token", json={"room": "session-42"})
    assert response.status_code == 401


def test_mint_token_returns_token_url_and_room(client: TestClient) -> None:
    response = client.post(
        "/livekit/token",
        json={"room": "session-42"},
        headers={"Authorization": f"Bearer {_user_jwt()}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["room"] == "session-42"
    assert body["identity"] == "user-abc"
    assert body["url"].startswith(("ws://", "wss://"))
    assert isinstance(body["token"], str)
    assert len(body["token"]) > 20


def test_mint_token_embeds_correct_identity_and_room(client: TestClient) -> None:
    """The issued LiveKit JWT must bind identity to user_id and scope to the requested room."""
    response = client.post(
        "/livekit/token",
        json={"room": "session-xyz"},
        headers={"Authorization": f"Bearer {_user_jwt(sub='u-777')}"},
    )
    assert response.status_code == 200
    lk_token = response.json()["token"]
    claims = jwt.decode(
        lk_token,
        os.environ["API_LIVEKIT_API_SECRET"],
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert claims["sub"] == "u-777"
    assert claims["iss"] == os.environ["API_LIVEKIT_API_KEY"]
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["room"] == "session-xyz"
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is True


def test_mint_token_rejects_empty_room(client: TestClient) -> None:
    response = client.post(
        "/livekit/token",
        json={"room": ""},
        headers={"Authorization": f"Bearer {_user_jwt()}"},
    )
    assert response.status_code == 422
