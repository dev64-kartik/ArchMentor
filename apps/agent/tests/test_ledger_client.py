"""Tests for the agent → API event ledger client.

httpx.MockTransport gives us a deterministic HTTP stub without spinning
up a server, so the tests assert retry behavior and header forwarding.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from archmentor_agent.ledger.client import LedgerClient, LedgerConfig


def _make_client(
    handler,  # type: ignore[no-untyped-def]
    *,
    retries: int = 2,
) -> LedgerClient:
    transport = httpx.MockTransport(handler)
    cfg = LedgerConfig(
        base_url="http://api.local",
        agent_token="test-secret",  # noqa: S106 — fixture value, not a real secret
        max_retries=retries,
    )
    return LedgerClient(
        cfg,
        client=httpx.AsyncClient(
            transport=transport,
            base_url=cfg.base_url,
            headers={"X-Agent-Token": cfg.agent_token},
        ),
    )


async def test_append_sends_expected_body_and_header() -> None:
    seen_headers: dict[str, str] = {}
    seen_url = ""
    seen_content = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url, seen_content
        seen_headers.update(dict(request.headers))
        seen_url = str(request.url)
        seen_content = request.content
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 1, "type": "utterance_ai"})

    client = _make_client(handler)
    sid = uuid4()
    ok = await client.append(
        session_id=sid,
        t_ms=1,
        event_type="utterance_ai",
        payload={"text": "hi"},
    )
    await client.aclose()

    assert ok is True
    assert f"/sessions/{sid}/events" in seen_url
    assert seen_headers["x-agent-token"] == "test-secret"
    assert b'"text":"hi"' in seen_content


async def test_append_retries_on_5xx_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"detail": "try again"})
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 0, "type": "utterance_ai"})

    client = _make_client(handler, retries=3)
    ok = await client.append(session_id=uuid4(), t_ms=0, event_type="utterance_ai", payload={})
    await client.aclose()

    assert ok is True
    assert attempts["n"] == 3


async def test_append_does_not_retry_on_4xx() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"detail": "session gone"})

    client = _make_client(handler, retries=5)
    ok = await client.append(session_id=uuid4(), t_ms=0, event_type="utterance_ai", payload={})
    await client.aclose()

    assert ok is False
    assert attempts["n"] == 1  # no retry for 4xx


async def test_append_drops_after_repeated_5xx() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="boom")

    client = _make_client(handler, retries=2)
    ok = await client.append(session_id=uuid4(), t_ms=0, event_type="utterance_ai", payload={})
    await client.aclose()

    assert ok is False
    assert attempts["n"] == 3  # 1 + 2 retries


async def test_append_survives_transport_errors() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("refused")
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 0, "type": "error"})

    client = _make_client(handler, retries=2)
    ok = await client.append(session_id=uuid4(), t_ms=0, event_type="error", payload={"msg": "x"})
    await client.aclose()

    assert ok is True
    assert attempts["n"] == 2


@pytest.mark.parametrize("event_type", ["utterance_candidate", "canvas_change"])
async def test_append_forwards_event_type(event_type: str) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content.decode())
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 0, "type": event_type})

    client = _make_client(handler)
    ok = await client.append(session_id=uuid4(), t_ms=0, event_type=event_type, payload={})
    await client.aclose()

    assert ok is True
    assert f'"type":"{event_type}"' in captured[0]
