"""End-to-end tests for the M4 Unit 7 phase-timer producer.

The producer is a per-session asyncio task on `MentorAgent`. We exercise
its single-tick logic (`_maybe_dispatch_phase_timer_tick`) against the
existing `_build_agent_under_test` harness so the assertions cover the
real Redis-load + RouterEvent dispatch path; the loop scaffolding
(`asyncio.sleep` cadence) is covered by a single `test_loop_*` case
that drives one tick under monkeypatched sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
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
from archmentor_agent.events import EventType
from archmentor_agent.main import (
    MentorAgent,
    build_brain_wiring,
)
from archmentor_agent.snapshots.client import SnapshotClient
from archmentor_agent.state.redis_store import RedisSessionStore
from archmentor_agent.state.session_state import (
    InterviewPhase,
    ProblemCard,
    SessionState,
)

SESSION_ID = UUID("11111111-2222-3333-4444-555555555555")


# ──────────────────────────────────────────────────────────────────────
# Test harness — minimal MentorAgent reuse
# ──────────────────────────────────────────────────────────────────────


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish_data(self, payload: str, *, topic: str) -> None:
        self.published.append((topic, payload))


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
        _ = session_id, t_ms
        self.appends.append((event_type, dict(payload)))
        return True

    async def aclose(self) -> None:
        return None


def _seed_state(*, phase: InterviewPhase, last_phase_change_s: int) -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="URL Shortener",
            statement_md="...",
            rubric_yaml="dimensions: []\n",
        ),
        system_prompt_version="m4-test",
        started_at=datetime(2026, 4, 27, tzinfo=UTC),
        phase=phase,
        last_phase_change_s=last_phase_change_s,
    )


def _build_agent(
    *,
    seed_state: SessionState,
    brain: FakeBrainClient | None = None,
) -> tuple[MentorAgent, FakeBrainClient]:
    brain = brain or FakeBrainClient()
    store = FakeSessionStore()
    store._states[SESSION_ID] = seed_state

    agent = MentorAgent(
        session_id=SESSION_ID,
        ledger=cast(Any, _FakeLedger()),
        room=cast(Any, _FakeRoom()),
        brain_enabled=True,
        brain=None,
    )
    wiring = build_brain_wiring(
        agent,
        brain=cast(BrainClient, brain),
        store=cast(RedisSessionStore, store),
        snapshot_client=cast(SnapshotClient, FakeSnapshotClient()),
        canvas_snapshot_client=cast(CanvasSnapshotClient, FakeCanvasSnapshotClient()),
    )
    agent.attach_brain(wiring)

    async def fake_say(text: str) -> None:
        _ = text

    agent._say = fake_say  # ty: ignore[invalid-assignment]

    def fake_start_streaming_say(deltas: AsyncIterator[str]) -> Any:
        async def _drain() -> None:
            async for _ in deltas:
                pass

        task = asyncio.create_task(_drain(), name="fake.streaming_say")

        class _FakeStreamingHandle:
            async def wait_for_playout(self) -> None:
                await task

        return _FakeStreamingHandle()

    agent._start_streaming_say = fake_start_streaming_say  # ty: ignore[invalid-assignment]
    agent._t0_ms = 0
    return agent, brain


def _set_clock(agent: MentorAgent, *, seconds: int) -> None:
    """Move the agent's session-relative clock forward by replacing _t0_ms.

    `_now_relative_ms` returns `monotonic - _t0_ms`, clamped to >= 0.
    Adjusting `_t0_ms` to `monotonic*1000 - target_ms` lets us simulate
    "session has been running for N seconds" without sleeping.
    """
    import time

    agent._t0_ms = int(time.monotonic() * 1000) - seconds * 1000


# ──────────────────────────────────────────────────────────────────────
# Single-tick dispatches
# ──────────────────────────────────────────────────────────────────────


async def test_no_dispatch_when_under_budget() -> None:
    """Elapsed below budget * 1.5 → no PHASE_TIMER fired."""
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    _set_clock(agent, seconds=100)  # INTRO budget=120, threshold=180

    assert agent._brain is not None
    pending_before = list(agent._brain.router._pending)
    await agent._maybe_dispatch_phase_timer_tick()
    assert list(agent._brain.router._pending) == pending_before


async def test_dispatch_when_over_budget_at_50pct() -> None:
    """INTRO budget = 120 s; 200 s elapsed = 67% over → dispatch tier 50."""
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    _set_clock(agent, seconds=200)

    assert agent._brain is not None
    await agent._maybe_dispatch_phase_timer_tick()

    pending = list(agent._brain.router._pending)
    assert len(pending) == 1
    event = pending[0]
    assert event.type is EventType.PHASE_TIMER
    assert event.payload == {
        "phase": "intro",
        "over_budget_pct_tier": 50,
    }


async def test_dispatch_emits_higher_tier_at_200pct() -> None:
    """3x the INTRO budget elapsed → tier 200 (the highest bucket)."""
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    _set_clock(agent, seconds=400)  # 400/120 - 1 = 233% → tier 200

    assert agent._brain is not None
    await agent._maybe_dispatch_phase_timer_tick()
    pending = list(agent._brain.router._pending)
    assert len(pending) == 1
    assert pending[0].payload["over_budget_pct_tier"] == 200


async def test_dedup_blocks_re_dispatch_within_window() -> None:
    """A second tick within 90 s of the first must NOT dispatch."""
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    assert agent._brain is not None

    _set_clock(agent, seconds=200)
    await agent._maybe_dispatch_phase_timer_tick()
    assert len(list(agent._brain.router._pending)) == 1

    # 30 s later — still inside the 90 s dedup window. Drain pending so
    # the next assertion is unambiguous about the (lack of) new dispatch.
    agent._brain.router._pending.clear()
    _set_clock(agent, seconds=230)
    await agent._maybe_dispatch_phase_timer_tick()
    assert list(agent._brain.router._pending) == []


async def test_dedup_releases_after_90s_window() -> None:
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    assert agent._brain is not None

    _set_clock(agent, seconds=200)
    await agent._maybe_dispatch_phase_timer_tick()
    agent._brain.router._pending.clear()

    # 100 s later — outside the 90 s gap. Same tier (still tier 50,
    # because 300/120 - 1 = 150%? wait that's tier 100). The bucket may
    # change too, but either way the dispatch should fire again.
    _set_clock(agent, seconds=300)
    await agent._maybe_dispatch_phase_timer_tick()
    assert len(list(agent._brain.router._pending)) == 1


async def test_phase_advance_resets_elapsed_anchor() -> None:
    """A fresh phase right before a tick must NOT immediately fire a nudge."""
    # Seed state already in CAPACITY (budget 300) with last_phase_change_s
    # only 30 s ago — so elapsed_in_phase = 30 s, well below 450 s threshold.
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.CAPACITY, last_phase_change_s=900)
    )
    assert agent._brain is not None

    # Total session elapsed = 930 s; in-phase elapsed = 30 s.
    _set_clock(agent, seconds=930)
    await agent._maybe_dispatch_phase_timer_tick()
    assert list(agent._brain.router._pending) == []


async def test_router_phase_advance_anchors_next_phase_at_now_ms() -> None:
    """End-to-end regression: when the brain dispatches a `phase_advance`,
    the router's `_apply_decision` passes `now_ms=t_ms` into
    `with_state_updates`, which sets `last_phase_change_s = t_ms // 1000`
    on the new phase.

    Previously ``elapsed_s`` (a dead field, always 0) was used as the
    anchor, so every post-INTRO phase nudge fired immediately. This
    test pins the live wall-clock anchor.
    """
    from archmentor_agent.brain.decision import BrainDecision
    from archmentor_agent.events import EventType, RouterEvent

    advance_decision = BrainDecision(
        decision="speak",
        priority="medium",
        confidence=0.9,
        reasoning="moving on",
        utterance="Let's talk capacity.",
        state_updates={"phase_advance": "capacity"},
    )
    brain = FakeBrainClient()
    brain.enqueue(advance_decision)
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.REQUIREMENTS, last_phase_change_s=0),
        brain=brain,
    )
    assert agent._brain is not None

    # Dispatch from the brain at t = 600 s.
    _set_clock(agent, seconds=600)
    await agent._brain.router.handle(
        RouterEvent(
            type=EventType.TURN_END,
            t_ms=600_000,
            payload={"text": "x"},
        )
    )
    await agent._brain.router.wait_for_idle()

    final_state = await agent._brain.store.load(SESSION_ID)
    assert final_state is not None
    assert final_state.phase is InterviewPhase.CAPACITY
    # Anchor jumped to the dispatch's wall-clock anchor (t_ms=600_000 → 600 s).
    assert final_state.last_phase_change_s == 600

    # CAPACITY budget = 300; elapsed_in_phase right after advance = 0.
    # Even at t = 700 s session elapsed (in-phase = 100 s), no nudge fires.
    _set_clock(agent, seconds=700)
    pending_before = len(agent._brain.router._pending)
    await agent._maybe_dispatch_phase_timer_tick()
    assert len(agent._brain.router._pending) == pending_before


async def test_no_state_in_redis_logs_and_skips() -> None:
    """Tick when Redis is empty — no crash, no dispatch."""
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    assert agent._brain is not None
    # Wipe the seeded state.
    agent._brain.store._states.pop(SESSION_ID, None)  # ty: ignore[unresolved-attribute]

    _set_clock(agent, seconds=200)
    await agent._maybe_dispatch_phase_timer_tick()
    assert list(agent._brain.router._pending) == []


# ──────────────────────────────────────────────────────────────────────
# Lifecycle — start + cancel via shutdown
# ──────────────────────────────────────────────────────────────────────


async def test_start_phase_timer_task_is_idempotent() -> None:
    """Calling `_start_phase_timer_task` twice spawns at most one task."""
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    agent._start_phase_timer_task()
    first = agent._phase_timer_task
    assert first is not None
    agent._start_phase_timer_task()
    assert agent._phase_timer_task is first
    # Cancel for a clean teardown.
    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first


async def test_shutdown_cancels_phase_timer_task() -> None:
    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    agent._start_phase_timer_task()
    task = agent._phase_timer_task
    assert task is not None
    assert not task.done()

    await agent.shutdown()
    assert task.done()
    # Cancellation is the expected outcome; an unexpected exception
    # would also have stopped the loop, so check both branches.
    assert task.cancelled() or task.exception() is None


async def test_shutdown_kill_switch_does_not_start_timer() -> None:
    """Kill-switch agents (brain_enabled=False) skip the phase-timer task."""
    agent = MentorAgent(
        session_id=SESSION_ID,
        ledger=cast(Any, _FakeLedger()),
        room=cast(Any, _FakeRoom()),
        brain_enabled=False,
        brain=None,
    )
    agent._start_phase_timer_task()
    assert agent._phase_timer_task is None
    # `shutdown()` is a no-op for the timer-cancel path.
    await agent.shutdown()


# ──────────────────────────────────────────────────────────────────────
# Cost-runaway property — fingerprint-skip suppresses repeats inside a tier
# ──────────────────────────────────────────────────────────────────────


async def test_repeated_ticks_inside_one_tier_are_throttle_skipped() -> None:
    """Brain stub returns stay_silent; repeated PHASE_TIMER dispatches at
    the same tier short-circuit via the cost-throttle's idempotent gate.

    Without bucketing, a 30 s tick cadence increments over_budget_pct
    monotonically and the fingerprint flips on every dispatch (defeating
    the cost throttle). With tier bucketing + the dedup window, repeated
    ticks at the same tier produce identical event payloads → identical
    fingerprints → `skipped_idempotent` short-circuits.
    """
    brain = FakeBrainClient()
    # First (real) dispatch: stay_silent → arms the fingerprint.
    brain.enqueue_stay_silent("over_budget")
    # If the throttle fails to skip, the second dispatch would consume
    # this — it should NOT be touched.
    brain.enqueue_stay_silent("should_not_be_called")

    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0),
        brain=brain,
    )
    assert agent._brain is not None

    # First tick: 200 s elapsed → tier 50; dispatch lands.
    _set_clock(agent, seconds=200)
    await agent._maybe_dispatch_phase_timer_tick()
    await agent._brain.router.wait_for_idle()

    # Second tick at 230 s: still inside tier 50 (over_budget_pct = 91 < 100).
    # Reset dedup so the producer would otherwise re-dispatch — but the
    # fingerprint gate should short-circuit because the brain inputs are
    # the same (state phase + payload tier identical).
    agent._phase_nudge_history.clear()
    _set_clock(agent, seconds=230)
    await agent._maybe_dispatch_phase_timer_tick()
    await agent._brain.router.wait_for_idle()

    # The brain stub got exactly one real call — the second was
    # short-circuited by the fingerprint gate.
    assert len(brain.calls) == 1
    assert agent._telemetry.skipped_idempotent_count == 1


async def test_phase_timer_loop_survives_per_tick_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised exception inside one tick must not kill the producer loop.

    Patches the *module-level* `asyncio.sleep` reference inside
    `archmentor_agent.main` (NOT the global `asyncio.sleep` — the
    pytest-asyncio event loop driver depends on the real one and a
    global patch deadlocks the test session). The stub fires the dispatch
    twice: the first raises; the second observes that the loop kept
    going and cancels via `CancelledError`.
    """
    from archmentor_agent import main as agent_main

    agent, _ = _build_agent(
        seed_state=_seed_state(phase=InterviewPhase.INTRO, last_phase_change_s=0)
    )
    _set_clock(agent, seconds=200)

    ticks: list[int] = []

    async def boom_then_cancel() -> None:
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise RuntimeError("simulated tick error")
        # Second tick: the loop survived. Raise CancelledError so the
        # outer try/except cleanly exits the loop. Calling
        # `task.cancel()` would only schedule the cancellation, and the
        # patched fast_sleep below never actually yields to the event
        # loop — so the cancellation would never get a chance to fire.
        raise asyncio.CancelledError

    agent._maybe_dispatch_phase_timer_tick = boom_then_cancel  # ty: ignore[invalid-assignment]

    sleep_calls: list[float] = []

    async def fast_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr(agent_main.asyncio, "sleep", fast_sleep)

    try:
        agent._start_phase_timer_task()
        task = agent._phase_timer_task
        assert task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        # Restore asyncio.sleep for any teardown that depends on it.
        monkeypatch.undo()

    assert sleep_calls
    assert sleep_calls[0] == 30.0
    # First tick raised → loop logged + slept again → second tick fired.
    assert len(ticks) == 2
