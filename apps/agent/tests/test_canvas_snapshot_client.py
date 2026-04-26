"""Tests for `archmentor_agent.canvas.client.CanvasSnapshotClient`.

Mirrors `test_snapshot_client.py` deliberately so any drift between the
two clients (retry shape, auth header, drop-vs-retry policy) is
visible side-by-side. If you change one, change the other.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
from archmentor_agent.canvas.client import CanvasSnapshotClient, CanvasSnapshotClientConfig


def _make_client(
    handler,  # type: ignore[no-untyped-def]
    *,
    retries: int = 2,
) -> CanvasSnapshotClient:
    transport = httpx.MockTransport(handler)
    cfg = CanvasSnapshotClientConfig(
        base_url="http://api.local",
        agent_token="test-secret",  # noqa: S106 — fixture, not a real secret
        max_retries=retries,
    )
    return CanvasSnapshotClient(
        cfg,
        client=httpx.AsyncClient(
            transport=transport,
            base_url=cfg.base_url,
            headers={"X-Agent-Token": cfg.agent_token},
        ),
    )


def _kwargs(session_id=None):  # type: ignore[no-untyped-def]
    return {
        "session_id": session_id or uuid4(),
        "t_ms": 12_000,
        "scene_json": {"elements": [{"id": "r1", "type": "rectangle"}]},
    }


async def test_append_sends_expected_body_and_header() -> None:
    seen_headers: dict[str, str] = {}
    seen_url = ""
    seen_content = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url, seen_content
        seen_headers.update(dict(request.headers))
        seen_url = str(request.url)
        seen_content = request.content
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 12_000})

    client = _make_client(handler)
    sid = uuid4()
    ok = await client.append(**_kwargs(session_id=sid))
    await client.aclose()

    assert ok is True
    assert f"/sessions/{sid}/canvas-snapshots" in seen_url
    assert seen_headers["x-agent-token"] == "test-secret"
    assert b'"t_ms":12000' in seen_content
    assert b"scene_json" in seen_content


async def test_append_retries_on_5xx_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"detail": "try again"})
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 0})

    client = _make_client(handler, retries=3)
    ok = await client.append(**_kwargs())
    await client.aclose()

    assert ok is True
    assert attempts["n"] == 3


async def test_append_does_not_retry_on_4xx() -> None:
    """409 (session ended) and 422 (files key forbidden) are permanent —
    retrying would just burn requests on a dead/misconfigured session."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(409, json={"detail": "session ended"})

    client = _make_client(handler, retries=5)
    ok = await client.append(**_kwargs())
    await client.aclose()

    assert ok is False
    assert attempts["n"] == 1


async def test_append_drops_after_repeated_5xx() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="boom")

    client = _make_client(handler, retries=2)
    ok = await client.append(**_kwargs())
    await client.aclose()

    assert ok is False
    assert attempts["n"] == 3


async def test_append_survives_transport_errors() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("refused")
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 0})

    client = _make_client(handler, retries=2)
    ok = await client.append(**_kwargs())
    await client.aclose()

    assert ok is True
    assert attempts["n"] == 2


async def test_append_drops_on_403_wrong_agent_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    client = _make_client(handler, retries=5)
    ok = await client.append(**_kwargs())
    await client.aclose()
    assert ok is False
