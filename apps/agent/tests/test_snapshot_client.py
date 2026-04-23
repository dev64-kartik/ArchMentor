"""Tests for `archmentor_agent.snapshots.client.SnapshotClient`.

Mirrors `test_ledger_client.py` — same `httpx.MockTransport` approach
so retry/backoff + header forwarding + 4xx-vs-5xx behavior can be
asserted without spinning up a server. Kept structurally parallel to
the ledger tests; if the two diverge, the retry policies have drifted
and at least one is wrong.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
from archmentor_agent.snapshots.client import SnapshotClient, SnapshotClientConfig


def _make_client(
    handler,  # type: ignore[no-untyped-def]
    *,
    retries: int = 2,
) -> SnapshotClient:
    transport = httpx.MockTransport(handler)
    cfg = SnapshotClientConfig(
        base_url="http://api.local",
        agent_token="test-secret",  # noqa: S106 — fixture, not a real secret
        max_retries=retries,
    )
    return SnapshotClient(
        cfg,
        client=httpx.AsyncClient(
            transport=transport,
            base_url=cfg.base_url,
            headers={"X-Agent-Token": cfg.agent_token},
        ),
    )


def _valid_kwargs(session_id=None):  # type: ignore[no-untyped-def]
    return {
        "session_id": session_id or uuid4(),
        "t_ms": 120_000,
        "session_state_json": {"phase": "requirements"},
        "event_payload_json": {"type": "turn_end"},
        "brain_output_json": {"decision": "stay_silent", "confidence": 0.7},
        "reasoning_text": "Low-signal reasoning placeholder.",
        "tokens_input": 500,
        "tokens_output": 80,
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
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 120_000})

    client = _make_client(handler)
    sid = uuid4()
    ok = await client.append(**_valid_kwargs(session_id=sid))
    await client.aclose()

    assert ok is True
    assert f"/sessions/{sid}/snapshots" in seen_url
    assert seen_headers["x-agent-token"] == "test-secret"
    assert b'"t_ms":120000' in seen_content
    # All four JSON fields + reasoning make it through the serializer.
    assert b"session_state_json" in seen_content
    assert b"event_payload_json" in seen_content
    assert b"brain_output_json" in seen_content
    assert b"reasoning_text" in seen_content


async def test_append_retries_on_5xx_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"detail": "try again"})
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 0})

    client = _make_client(handler, retries=3)
    ok = await client.append(**_valid_kwargs())
    await client.aclose()

    assert ok is True
    assert attempts["n"] == 3


async def test_append_does_not_retry_on_4xx() -> None:
    """409 (session ended) and 413 (payload too large) are permanent —
    retrying would just burn requests on a dead session."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(409, json={"detail": "session ended"})

    client = _make_client(handler, retries=5)
    ok = await client.append(**_valid_kwargs())
    await client.aclose()

    assert ok is False
    assert attempts["n"] == 1


async def test_append_drops_after_repeated_5xx() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="boom")

    client = _make_client(handler, retries=2)
    ok = await client.append(**_valid_kwargs())
    await client.aclose()

    assert ok is False
    assert attempts["n"] == 3  # 1 + 2 retries


async def test_append_survives_transport_errors() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("refused")
        return httpx.Response(201, json={"id": str(uuid4()), "t_ms": 0})

    client = _make_client(handler, retries=2)
    ok = await client.append(**_valid_kwargs())
    await client.aclose()

    assert ok is True
    assert attempts["n"] == 2


async def test_append_returns_false_on_permanent_4xx_403() -> None:
    """Wrong agent token → 403. No retries; ledger client handles this
    identically, and the agent's shutdown drain relies on the client
    returning rather than looping forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    client = _make_client(handler, retries=5)
    ok = await client.append(**_valid_kwargs())
    await client.aclose()
    assert ok is False
