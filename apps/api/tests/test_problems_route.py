"""Tests for GET /problems and GET /problems/{slug}."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import archmentor_api.models  # noqa: F401  — registers tables
import jwt
import pytest
from archmentor_api.db import get_db_session
from archmentor_api.main import app
from archmentor_api.models.problem import Problem
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
def user_headers(engine: Engine) -> dict[str, str]:
    """Mint a JWT for a real seeded user, mirroring GoTrue's HS256 shape."""
    user_id = uuid4()
    with Session(engine) as db:
        db.add(User(id=user_id, email="candidate@example.com"))
        db.commit()
    token = jwt.encode(
        {
            "sub": str(user_id),
            "email": "candidate@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "iss": os.environ["API_JWT_ISSUER"],
        },
        os.environ["API_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_problem(engine: Engine, *, slug: str, title: str, difficulty: str = "medium") -> None:
    with Session(engine) as db:
        db.add(
            Problem(
                slug=slug,
                title=title,
                statement_md=f"# {title}",
                difficulty=difficulty,
                rubric_yaml="dimensions: []",
                ideal_solution_md="...",
                seniority_calibration_json={},
            )
        )
        db.commit()


def test_list_problems_returns_seeded(
    client: TestClient, engine: Engine, user_headers: dict[str, str]
) -> None:
    _seed_problem(engine, slug="url-shortener", title="Design a URL shortener")
    response = client.get("/problems", headers=user_headers)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert item["slug"] == "url-shortener"
    assert item["title"] == "Design a URL shortener"
    assert item["difficulty"] == "medium"
    assert "version" in item
    # List endpoint is the picker payload — heavy fields stay out.
    assert "statement_md" not in item
    assert "rubric_yaml" not in item


def test_list_problems_orders_by_slug_asc(
    client: TestClient, engine: Engine, user_headers: dict[str, str]
) -> None:
    _seed_problem(engine, slug="b-second", title="Second")
    _seed_problem(engine, slug="a-first", title="First")
    response = client.get("/problems", headers=user_headers)
    assert response.status_code == 200
    slugs = [p["slug"] for p in response.json()]
    assert slugs == ["a-first", "b-second"]


def test_list_problems_empty_returns_200_empty_list(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    response = client.get("/problems", headers=user_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_list_problems_requires_auth(client: TestClient) -> None:
    response = client.get("/problems")
    assert response.status_code == 401


def test_get_problem_returns_full_row(
    client: TestClient, engine: Engine, user_headers: dict[str, str]
) -> None:
    _seed_problem(engine, slug="url-shortener", title="Design a URL shortener")
    response = client.get("/problems/url-shortener", headers=user_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "url-shortener"
    assert body["title"] == "Design a URL shortener"
    assert body["statement_md"] == "# Design a URL shortener"
    assert body["rubric_yaml"] == "dimensions: []"
    assert body["difficulty"] == "medium"
    assert body["version"] == 1


def test_get_problem_unknown_slug_returns_404(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    response = client.get("/problems/does-not-exist", headers=user_headers)
    assert response.status_code == 404


def test_get_problem_requires_auth(client: TestClient) -> None:
    response = client.get("/problems/url-shortener")
    assert response.status_code == 401
