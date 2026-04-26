"""Tests for the body-size ASGI middleware on agent ingest routes.

The middleware enforces 16 KiB on `/events` and 256 KiB on `/snapshots`
+ `/canvas-snapshots`. Rejection happens *before* Pydantic deserialization;
in-handler caps in `routes/sessions.py` stay as defense-in-depth.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import archmentor_api.models  # noqa: F401  — registers tables
import pytest
from archmentor_api.db import get_db_session
from archmentor_api.main import app
from archmentor_api.middleware.body_size import (
    EVENT_BODY_CAP_BYTES,
    SNAPSHOT_BODY_CAP_BYTES,
    BodySizeLimitMiddleware,
    _content_length,
)
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
def session_id(engine: Engine) -> UUID:
    with Session(engine) as db:
        user = User(id=uuid4(), email=f"c-{uuid4().hex[:6]}@example.com")
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


class TestEventCap:
    def test_small_event_passes(self, client: TestClient, session_id: UUID) -> None:
        response = client.post(
            f"/sessions/{session_id}/events",
            json={
                "t_ms": 0,
                "type": "utterance_candidate",
                "payload_json": {"text": "small"},
            },
            headers=_agent_headers(),
        )
        assert response.status_code == 201

    def test_oversized_event_rejected_by_middleware(
        self, client: TestClient, session_id: UUID
    ) -> None:
        """Body well over 16 KiB must 413 from middleware before Pydantic parses.

        The shape of the JSON body produced here is comfortably above the
        16 KiB cap — the middleware reads `Content-Length` and short-circuits
        without invoking the route handler.
        """
        # Build a payload that's clearly over the cap. The full request body
        # (JSON-wrapped) will be larger than the inner string by a small
        # constant — so 17 KiB inner string ≫ 16 KiB cap.
        huge = {"text": "x" * (17 * 1024)}
        response = client.post(
            f"/sessions/{session_id}/events",
            json={
                "t_ms": 0,
                "type": "utterance_candidate",
                "payload_json": huge,
            },
            headers=_agent_headers(),
        )
        assert response.status_code == 413
        assert "max" in response.json()["detail"].lower()


class TestSnapshotCap:
    def test_under_snapshot_cap_passes(self, client: TestClient, session_id: UUID) -> None:
        # Comfortably under 256 KiB: 200 KiB of reasoning text.
        body = {
            "t_ms": 0,
            "session_state_json": {},
            "event_payload_json": {},
            "brain_output_json": {},
            "reasoning_text": "x" * (200 * 1024),
            "tokens_input": 0,
            "tokens_output": 0,
        }
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=body,
            headers=_agent_headers(),
        )
        assert response.status_code == 201

    def test_over_snapshot_cap_rejected_by_middleware(
        self, client: TestClient, session_id: UUID
    ) -> None:
        body = {
            "t_ms": 0,
            "session_state_json": {},
            "event_payload_json": {},
            "brain_output_json": {},
            # 300 KiB > 256 KiB cap.
            "reasoning_text": "x" * (300 * 1024),
            "tokens_input": 0,
            "tokens_output": 0,
        }
        response = client.post(
            f"/sessions/{session_id}/snapshots",
            json=body,
            headers=_agent_headers(),
        )
        assert response.status_code == 413


class TestUngatedRoutes:
    """Routes outside `/sessions/{id}/(events|snapshots|canvas-snapshots)` must not be capped."""

    def test_health_route_uncapped(self, client: TestClient) -> None:
        # The health check uses GET (no body), but verifying it still responds
        # confirms the middleware isn't accidentally short-circuiting unmatched paths.
        response = client.get("/health")
        assert response.status_code == 200


class TestContentLengthLie:
    """Direct-ASGI tests for the Content-Length lie attack.

    A client declaring a small C-L but sending a large actual body must
    still be rejected. `TestClient` always sets an accurate C-L, so these
    tests drive the middleware via a fake ASGI transport.
    """

    @pytest.mark.asyncio
    async def test_content_length_lie_oversized_body_rejected(self) -> None:
        """POST with Content-Length: 100 but 10 KiB body must 413."""

        async def inner_app(scope: dict, receive, send) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("inner app must not be invoked when middleware aborts")

        middleware = BodySizeLimitMiddleware(inner_app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/sessions/{uuid4()}/events",
            # Declare a small body that's within the 16 KiB cap.
            "headers": [(b"content-length", b"100")],
        }
        # But actually send 10 KiB — a C-L lie.
        chunks = [
            {"type": "http.request", "body": b"x" * (10 * 1024), "more_body": False},
        ]
        chunk_iter = iter(chunks)

        async def receive() -> dict:
            return next(chunk_iter)

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        await middleware(scope, receive, send)
        statuses = [m.get("status") for m in sent if m["type"] == "http.response.start"]
        assert statuses == [413]

    @pytest.mark.asyncio
    async def test_content_length_within_cap_streamed_count_matches_declared(self) -> None:
        """C-L: 50 with exactly 50 bytes body passes through to the inner app."""
        received_chunks: list[bytes] = []

        async def inner_app(scope: dict, receive, send) -> None:  # type: ignore[no-untyped-def]
            while True:
                msg = await receive()
                if msg["type"] == "http.request":
                    received_chunks.append(msg.get("body", b""))
                    if not msg.get("more_body", False):
                        break
            await send(
                {
                    "type": "http.response.start",
                    "status": 201,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        middleware = BodySizeLimitMiddleware(inner_app)
        body_bytes = b"x" * 50
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/sessions/{uuid4()}/events",
            "headers": [(b"content-length", str(len(body_bytes)).encode("ascii"))],
        }
        chunks = [
            {"type": "http.request", "body": body_bytes, "more_body": False},
        ]
        chunk_iter = iter(chunks)

        async def receive() -> dict:
            return next(chunk_iter)

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        await middleware(scope, receive, send)
        statuses = [m.get("status") for m in sent if m["type"] == "http.response.start"]
        assert statuses == [201]
        assert b"".join(received_chunks) == body_bytes

    @pytest.mark.asyncio
    async def test_content_length_within_cap_actual_smaller_passes(self) -> None:
        """C-L: 100 but only 50 bytes sent — under-counting is fine; only over-counting is a lie."""
        received_chunks: list[bytes] = []

        async def inner_app(scope: dict, receive, send) -> None:  # type: ignore[no-untyped-def]
            while True:
                msg = await receive()
                if msg["type"] == "http.request":
                    received_chunks.append(msg.get("body", b""))
                    if not msg.get("more_body", False):
                        break
            await send(
                {
                    "type": "http.response.start",
                    "status": 201,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        middleware = BodySizeLimitMiddleware(inner_app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/sessions/{uuid4()}/events",
            # Declare 100 bytes but only send 50.
            "headers": [(b"content-length", b"100")],
        }
        chunks = [
            {"type": "http.request", "body": b"x" * 50, "more_body": False},
        ]
        chunk_iter = iter(chunks)

        async def receive() -> dict:
            return next(chunk_iter)

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        await middleware(scope, receive, send)
        statuses = [m.get("status") for m in sent if m["type"] == "http.response.start"]
        assert statuses == [201]


class TestStreamedBodyFallback:
    """Direct-ASGI tests for chunked / Content-Length-less requests.

    `TestClient` always sends a `Content-Length` header, so these tests
    drive the middleware via a fake ASGI app.
    """

    @pytest.mark.asyncio
    async def test_chunked_request_under_cap_passes_through(self) -> None:
        received_chunks: list[bytes] = []

        async def inner_app(scope: dict, receive, send) -> None:  # type: ignore[no-untyped-def]
            assert scope["type"] == "http"
            while True:
                msg = await receive()
                if msg["type"] == "http.request":
                    received_chunks.append(msg.get("body", b""))
                    if not msg.get("more_body", False):
                        break
            await send(
                {
                    "type": "http.response.start",
                    "status": 201,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        middleware = BodySizeLimitMiddleware(inner_app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/sessions/{uuid4()}/events",
            "headers": [(b"transfer-encoding", b"chunked")],
        }
        # Two small chunks well under 16 KiB.
        chunks = [
            {"type": "http.request", "body": b"part-1-", "more_body": True},
            {"type": "http.request", "body": b"part-2", "more_body": False},
        ]
        chunk_iter = iter(chunks)

        async def receive() -> dict:
            return next(chunk_iter)

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        await middleware(scope, receive, send)

        # Middleware did not 413 (no http.response.start with status=413).
        statuses = [m.get("status") for m in sent if m["type"] == "http.response.start"]
        assert statuses == [201]
        # Inner app saw all chunks in order.
        assert b"".join(received_chunks) == b"part-1-part-2"

    @pytest.mark.asyncio
    async def test_chunked_request_over_cap_rejected(self) -> None:
        async def inner_app(scope: dict, receive, send) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("inner app must not be invoked when middleware aborts")

        middleware = BodySizeLimitMiddleware(inner_app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/sessions/{uuid4()}/events",
            "headers": [(b"transfer-encoding", b"chunked")],
        }
        # First chunk is under the cap, second pushes us over.
        chunks = [
            {"type": "http.request", "body": b"x" * 10_000, "more_body": True},
            {
                "type": "http.request",
                "body": b"x" * (EVENT_BODY_CAP_BYTES + 1),
                "more_body": False,
            },
        ]
        chunk_iter = iter(chunks)

        async def receive() -> dict:
            return next(chunk_iter)

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        await middleware(scope, receive, send)

        statuses = [m.get("status") for m in sent if m["type"] == "http.response.start"]
        assert statuses == [413]


def test_cap_constants_are_consistent() -> None:
    """Document the cap values so a reviewer notices a silent change."""
    assert EVENT_BODY_CAP_BYTES == 16 * 1024
    assert SNAPSHOT_BODY_CAP_BYTES == 256 * 1024


class TestContentLengthParsing:
    """Unit tests for `_content_length` — header normalisation and negative values."""

    def test_negative_content_length_treated_as_missing(self) -> None:
        """Content-Length: -1 is invalid per RFC 9110; treat as absent so the
        streaming-cap path runs instead of the fast-reject path."""
        scope = {"headers": [(b"content-length", b"-1")]}
        assert _content_length(scope) is None

    def test_mixed_case_content_length_header_parsed(self) -> None:
        """ASGI servers other than uvicorn may not lowercase header names.
        `Content-LENGTH: 100` must still be recognised."""
        scope = {"headers": [(b"Content-LENGTH", b"100")]}
        assert _content_length(scope) == 100

    def test_normal_content_length_parsed(self) -> None:
        scope = {"headers": [(b"content-length", b"512")]}
        assert _content_length(scope) == 512

    def test_missing_content_length_returns_none(self) -> None:
        scope = {"headers": [(b"accept", b"application/json")]}
        assert _content_length(scope) is None

    def test_non_integer_content_length_returns_none(self) -> None:
        scope = {"headers": [(b"content-length", b"abc")]}
        assert _content_length(scope) is None
