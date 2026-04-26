"""Tests for `MentorAgent.on_canvas_scene_payload` (Unit 9).

Covers the canvas-scene → router pipeline: rate limiting, malformed
JSON handling, server-side `files` strip (R17), CAS apply (R23),
ledger event with `parsed_text` (R21), and snapshot scheduling.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from _helpers import (
    FakeBrainClient,
    FakeCanvasSnapshotClient,
    FakeSessionStore,
    FakeSnapshotClient,
)
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.canvas.client import CanvasSnapshotClient
from archmentor_agent.config import reset_settings_cache
from archmentor_agent.main import (
    MentorAgent,
    build_brain_wiring,
)
from archmentor_agent.snapshots.client import SnapshotClient
from archmentor_agent.state.redis_store import RedisSessionStore
from archmentor_agent.state.session_state import (
    ProblemCard,
    SessionState,
)

SESSION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    reset_settings_cache()


class _FakeLedger:
    def __init__(self) -> None:
        self.appends: list[tuple[str, dict[str, object]]] = []

    async def append(
        self,
        *,
        session_id: UUID,
        t_ms: int,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        _ = session_id
        _ = t_ms
        self.appends.append((event_type, dict(payload)))
        return True

    async def aclose(self) -> None:
        return None


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish_data(self, payload: str, *, topic: str) -> None:
        self.published.append((topic, payload))


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


def _seed_state() -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="URL Shortener",
            statement_md="Design a URL shortener.",
            rubric_yaml="dimensions: []\n",
        ),
        system_prompt_version="m2-test",
        started_at=datetime(2026, 4, 25, tzinfo=UTC),
        elapsed_s=0,
        remaining_s=2700,
        cost_cap_usd=5.0,
    )


def _make_agent(
    *,
    brain: FakeBrainClient | None = None,
    store: FakeSessionStore | None = None,
    snapshots: FakeSnapshotClient | None = None,
    canvas_snapshots: FakeCanvasSnapshotClient | None = None,
    seed_state: SessionState | None = None,
) -> tuple[
    MentorAgent,
    _FakeLedger,
    FakeBrainClient,
    FakeSessionStore,
    FakeSnapshotClient,
    FakeCanvasSnapshotClient,
]:
    ledger = _FakeLedger()
    room = _FakeRoom()
    brain = brain or FakeBrainClient()
    store = store or FakeSessionStore()
    snapshots = snapshots or FakeSnapshotClient()
    canvas_snapshots = canvas_snapshots or FakeCanvasSnapshotClient()
    state = seed_state or _seed_state()
    store._states[SESSION_ID] = state

    agent = MentorAgent(
        session_id=SESSION_ID,
        ledger=cast(Any, ledger),
        room=cast(Any, room),
        brain_enabled=True,
        brain=None,
    )
    wiring = build_brain_wiring(
        agent,
        brain=cast(BrainClient, brain),
        store=cast(RedisSessionStore, store),
        snapshot_client=cast(SnapshotClient, snapshots),
        canvas_snapshot_client=cast(CanvasSnapshotClient, canvas_snapshots),
    )
    agent.attach_brain(wiring)
    agent._t0_ms = 0
    return agent, ledger, brain, store, snapshots, canvas_snapshots


def _scene_payload(*elements: dict[str, Any], t_ms: int = 1000) -> str:
    return json.dumps(
        {
            "t_ms": t_ms,
            "scene_fingerprint": "fp-abc",
            "scene_json": {"elements": list(elements), "appState": {}},
        }
    )


def _labeled_rect(eid: str, label: str, x: int = 0) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"id": eid, "type": "rectangle", "x": x, "y": 0, "width": 100, "height": 50},
        {
            "id": f"{eid}-lbl",
            "type": "text",
            "x": x,
            "y": 0,
            "width": 80,
            "height": 20,
            "text": label,
            "containerId": eid,
        },
    )


async def _drain_tasks(agent: MentorAgent) -> None:
    import asyncio

    if agent._snapshot_tasks:
        await asyncio.gather(*agent._snapshot_tasks, return_exceptions=True)
    if agent._canvas_tasks:
        await asyncio.gather(*agent._canvas_tasks, return_exceptions=True)
    if agent._ledger_tasks:
        await asyncio.gather(*agent._ledger_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_happy_path_dispatches_router_event_and_snapshots_and_ledgers() -> None:
    brain = FakeBrainClient()
    brain.enqueue_speak("Tell me about your indexing.", confidence=0.9)
    agent, ledger, _, store, _, canvas_snapshots = _make_agent(brain=brain)

    rect, label = _labeled_rect("api", "API Gateway")
    await agent.on_canvas_scene_payload(_scene_payload(rect, label))
    await _drain_tasks(agent)

    # 1 brain call dispatched, 1 canvas snapshot posted.
    assert len(brain.calls) == 1
    canvas_event = brain.calls[0].event
    assert canvas_event["type"] == "canvas_change"
    assert "<label>API Gateway</label>" in canvas_event["scene_text"]
    assert canvas_event["scene_fingerprint"] == "fp-abc"

    assert len(canvas_snapshots.posts) == 1
    assert canvas_snapshots.posts[0].t_ms == 1000

    # Ledger has the canvas_change row with parsed_text per R21.
    canvas_events = [p for et, p in ledger.appends if et == "canvas_change"]
    assert len(canvas_events) == 1
    assert "<label>API Gateway</label>" in canvas_events[0]["parsed_text"]
    assert canvas_events[0]["scene_fingerprint"] == "fp-abc"

    # R23: canvas_state.description applied to Redis BEFORE the brain call.
    persisted = store._states[SESSION_ID]
    assert "<label>API Gateway</label>" in persisted.canvas_state.description
    assert persisted.canvas_state.last_change_s == 1


@pytest.mark.asyncio
async def test_malformed_json_writes_canvas_parse_error_no_dispatch() -> None:
    agent, ledger, brain, _, _, canvas_snapshots = _make_agent()

    await agent.on_canvas_scene_payload("not json {{{")
    await _drain_tasks(agent)

    assert brain.calls == []
    assert canvas_snapshots.posts == []
    parse_errors = [p for et, p in ledger.appends if et == "canvas_parse_error"]
    assert len(parse_errors) == 1
    assert "ValueError" in parse_errors[0]["error"] or "JSONDecodeError" in parse_errors[0]["error"]


@pytest.mark.asyncio
async def test_top_level_non_dict_writes_canvas_parse_error() -> None:
    agent, ledger, brain, _, _, _ = _make_agent()
    await agent.on_canvas_scene_payload(json.dumps([1, 2, 3]))
    await _drain_tasks(agent)
    assert brain.calls == []
    assert any(et == "canvas_parse_error" for et, _ in ledger.appends)


@pytest.mark.asyncio
async def test_files_field_stripped_server_side() -> None:
    """R17 server-side enforcement — `files` never reaches the API."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ok")
    agent, _, _, _, _, canvas_snapshots = _make_agent(brain=brain)

    payload = json.dumps(
        {
            "t_ms": 1000,
            "scene_fingerprint": "fp",
            "scene_json": {
                "elements": [],
                "files": {"img1": "base64-data"},
            },
        }
    )
    await agent.on_canvas_scene_payload(payload)
    await _drain_tasks(agent)

    assert len(canvas_snapshots.posts) == 1
    assert "files" not in canvas_snapshots.posts[0].scene_json


@pytest.mark.asyncio
async def test_rate_limit_drops_excess_events_within_window() -> None:
    """R22 / Q9: 60 events / 60 s. Beyond that, drop until the window slides."""
    brain = FakeBrainClient()
    for _ in range(60):
        brain.enqueue_stay_silent("ok")
    agent, ledger, _, _, _, canvas_snapshots = _make_agent(brain=brain)

    rect, label = _labeled_rect("a", "A")
    for i in range(70):
        # Vary t_ms so they're observable in order, but real time is
        # the limiter. The handler uses time.monotonic for the window.
        await agent.on_canvas_scene_payload(_scene_payload(rect, label, t_ms=i * 10))
    await _drain_tasks(agent)

    # Exactly 60 dispatches reached the router; the next 10 were rate-limited.
    canvas_events = [p for et, p in ledger.appends if et == "canvas_change"]
    assert len(canvas_events) == 60
    assert len(canvas_snapshots.posts) == 60


@pytest.mark.asyncio
async def test_missing_scene_json_writes_parse_error() -> None:
    agent, ledger, brain, _, _, _ = _make_agent()
    await agent.on_canvas_scene_payload(json.dumps({"t_ms": 1000}))
    await _drain_tasks(agent)
    assert brain.calls == []
    assert any(et == "canvas_parse_error" for et, _ in ledger.appends)


@pytest.mark.asyncio
async def test_brain_disabled_drops_silently() -> None:
    """Kill-switch path has no router to dispatch to."""
    ledger = _FakeLedger()
    room = _FakeRoom()
    agent = MentorAgent(
        session_id=SESSION_ID,
        ledger=cast(Any, ledger),
        room=cast(Any, room),
        brain_enabled=False,
        brain=None,
    )
    agent._t0_ms = 0

    rect, label = _labeled_rect("a", "A")
    # Must not raise even though there's no brain wiring.
    await agent.on_canvas_scene_payload(_scene_payload(rect, label))


@pytest.mark.asyncio
async def test_canvas_change_priority_is_high() -> None:
    from archmentor_agent.events.types import Priority

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ok")
    agent, _, _, _, _, _ = _make_agent(brain=brain)
    assert agent._brain is not None

    # Spy on the router's pending list at handle time.
    captured: list[Any] = []
    real_handle = agent._brain.router.handle

    async def spy(event: Any) -> None:
        captured.append(event)
        await real_handle(event)

    agent._brain.router.handle = spy  # ty: ignore[invalid-assignment]

    rect, label = _labeled_rect("a", "A")
    await agent.on_canvas_scene_payload(_scene_payload(rect, label))
    await _drain_tasks(agent)

    assert len(captured) == 1
    assert captured[0].priority is Priority.HIGH


@pytest.mark.asyncio
async def test_no_baseline_state_logs_distinct_key_and_still_dispatches() -> None:
    """#26: CanvasNoBaselineStateError (current is None) must log
    `agent.canvas.no_baseline_state`, not `agent.canvas.cas_exhausted`,
    and must NOT block the canvas_change ledger row.
    """
    import structlog.testing

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ok")
    store = FakeSessionStore()
    # Intentionally DO NOT seed any state — store._states is empty, so
    # the mutator receives current=None and raises CanvasNoBaselineStateError.
    agent, ledger, _, _, _, _ = _make_agent(brain=brain, store=store)
    # Remove the state that _make_agent seeded so the mutator sees None.
    store._states.clear()

    rect, label = _labeled_rect("a", "A")
    with structlog.testing.capture_logs() as captured:
        await agent.on_canvas_scene_payload(_scene_payload(rect, label))
        await _drain_tasks(agent)

    events = [e.get("event", "") for e in captured]
    # Distinct no-baseline log key must appear; CAS-exhausted key must NOT.
    assert any("no_baseline_state" in e for e in events), (
        f"expected 'no_baseline_state' in log events; got: {events}"
    )
    assert not any("cas_exhausted" in e for e in events), (
        f"unexpected 'cas_exhausted' in log for no-baseline path: {events}"
    )
    # Canvas change ledger row still written despite the CAS failure.
    canvas_events = [p for et, p in ledger.appends if et == "canvas_change"]
    assert len(canvas_events) == 1
