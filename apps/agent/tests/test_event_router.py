"""Event-router invariant + behavior tests.

Test-first per the plan's Execution note: the router is the piece
most likely to be subtly wrong (cancellation races, lost events,
double-dispatch). These scenarios cover invariants I1, I2, I3 plus
cost guard, schema-violation escalation, and confidence gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import anthropic
import pytest
from _helpers import FakeBrainClient, FakeSessionStore, FakeSnapshotClient
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision, BrainUsage
from archmentor_agent.events import EventRouter, EventType, RouterEvent
from archmentor_agent.queue import SpeechCheckGate, UtteranceQueue
from archmentor_agent.snapshots.client import SnapshotClient
from archmentor_agent.state.redis_store import (
    RedisCasExhaustedError,
    RedisSessionStore,
)
from archmentor_agent.state.session_state import (
    InterviewPhase,
    ProblemCard,
    SessionState,
)

SESSION_ID = UUID("11111111-2222-3333-4444-555555555555")


class _FakeClock:
    def __init__(self, t0_ms: int = 0) -> None:
        self.t = t0_ms

    def now(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


def _make_state(*, cost_usd_total: float = 0.0, cost_cap_usd: float = 5.0) -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="URL Shortener",
            statement_md="Design a URL shortener.",
            rubric_yaml="dimensions: []\n",
        ),
        system_prompt_version="m2-test",
        started_at=datetime(2026, 4, 22, tzinfo=UTC),
        elapsed_s=0,
        remaining_s=2700,
        phase=InterviewPhase.REQUIREMENTS,
        cost_usd_total=cost_usd_total,
        cost_cap_usd=cost_cap_usd,
    )


def _make_router(
    *,
    brain: FakeBrainClient | None = None,
    store: FakeSessionStore | None = None,
    snapshots: FakeSnapshotClient | None = None,
    clock: _FakeClock | None = None,
    queue: UtteranceQueue | None = None,
    gate: SpeechCheckGate | None = None,
    log_events: list[tuple[str, dict[str, Any]]] | None = None,
    seed_state: SessionState | None = None,
) -> tuple[
    EventRouter,
    FakeBrainClient,
    FakeSessionStore,
    FakeSnapshotClient,
    UtteranceQueue,
    list[tuple[str, dict[str, Any]]],
    list[asyncio.Task[Any]],
]:
    brain = brain or FakeBrainClient()
    store = store or FakeSessionStore()
    snapshots = snapshots or FakeSnapshotClient()
    clock = clock or _FakeClock()
    queue = queue or UtteranceQueue(clock.now)
    gate = gate or SpeechCheckGate(clock.now)
    log_events = log_events if log_events is not None else []

    state = seed_state if seed_state is not None else _make_state()
    asyncio.get_event_loop()  # ensure loop exists
    # Seed Redis with a baseline state so the router doesn't bail.
    # We do this synchronously by reaching into the fake.
    store._states[SESSION_ID] = state

    snapshot_tasks: list[asyncio.Task[Any]] = []

    def schedule(coro: Any) -> None:
        snapshot_tasks.append(asyncio.create_task(coro))

    def log(event_type: str, payload: dict[str, Any]) -> None:
        log_events.append((event_type, dict(payload)))

    router = EventRouter(
        session_id=SESSION_ID,
        brain=cast(BrainClient, brain),
        store=cast(RedisSessionStore, store),
        snapshot_client=cast(SnapshotClient, snapshots),
        snapshot_scheduler=schedule,
        utterance_queue=queue,
        gate=gate,
        log_event=log,
        now_ms=clock.now,
    )
    return router, brain, store, snapshots, queue, log_events, snapshot_tasks


async def _await_loop(router: EventRouter) -> None:
    task = router._in_flight
    if task is None:
        return
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _await_snapshots(snapshot_tasks: list[asyncio.Task[Any]]) -> None:
    if snapshot_tasks:
        await asyncio.gather(*snapshot_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_single_turn_end_runs_one_brain_call_and_pushes_utterance() -> None:
    brain = FakeBrainClient()
    brain.enqueue_speak("Tell me about your data model.", confidence=0.9)
    router, _, _, snapshots, queue, log_events, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "hello"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 1
    popped = queue.pop_if_fresh()
    assert popped is not None
    assert popped.text == "Tell me about your data model."
    assert len(snapshots.posts) == 1
    decision_events = [e for e in log_events if e[0] == "brain_decision"]
    assert len(decision_events) == 1
    assert decision_events[0][1]["decision"] == "speak"


@pytest.mark.asyncio
async def test_concurrent_turn_ends_coalesce_into_one_call() -> None:
    brain = FakeBrainClient(delay_s=0.05)
    brain.enqueue_speak("first response")
    brain.enqueue_speak("second response")
    router, _, _, snapshots, _, _, snap_tasks = _make_router(brain=brain)

    # Fire one turn_end, yield long enough for the dispatch task to
    # enter `await brain.decide` (the sleep below outlives the loop's
    # own scheduling tick), then pile two more events on top. They land
    # in pending and coalesce into a single follow-up call.
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "a"}))
    await asyncio.sleep(0)
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_100, payload={"text": "b"}))
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_200, payload={"text": "c"}))

    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 2
    # Second call's event payload reflects the coalesced batch.
    second_event = brain.calls[1].event
    assert second_event["type"] == "turn_end"
    assert second_event["transcripts"] == ["b", "c"]
    assert len(snapshots.posts) == 2


@pytest.mark.asyncio
async def test_handle_returns_immediately_for_non_owner() -> None:
    """Second concurrent handle() must not block on the first dispatch."""
    brain = FakeBrainClient(delay_s=1.0)
    brain.enqueue_speak("slow")
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "a"}))
    second_started = asyncio.get_event_loop().time()
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_100, payload={"text": "b"}))
    second_ended = asyncio.get_event_loop().time()

    # Should return almost instantly — under 100ms even on a busy box.
    assert (second_ended - second_started) < 0.1

    await router.cancel_in_flight()
    await _await_snapshots(snap_tasks)


@pytest.mark.asyncio
async def test_cancel_in_flight_preserves_pending_batch_invariant_i2() -> None:
    """Cancellation during the brain call re-prepends the batch to pending.

    The next `handle(...)` then dispatches the preserved events.
    """
    brain = FakeBrainClient(delay_s=10.0)  # Long enough to cancel mid-call
    # Only one decision queued: the cancelled call never reaches the
    # post-await `popleft`, so the post-cancel dispatch is the first
    # call to actually return.
    brain.enqueue_speak("finally responded")
    router, _, _, _, queue, _, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "first"}))
    # Give the dispatch task time to enter `await brain.decide` —
    # tested at 0.05s on a busy-CI box without flake.
    await asyncio.sleep(0.05)
    await router.cancel_in_flight()

    # After cancellation, the router must be quiescent: no in-flight
    # task, dispatching flag cleared, and the batch back in pending.
    assert router._dispatching is False
    assert router._in_flight is None
    assert len(router._pending) == 1

    # Trigger a fresh dispatch — it should pick up the preserved event
    # plus our new one, coalesce, and call the brain once.
    brain.delay_s = 0  # the second call returns instantly
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=2_000, payload={"text": "second"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # Two brain calls total: the cancelled one (recorded but never
    # returned) and the post-cancel one.
    assert len(brain.calls) == 2
    second_call_event = brain.calls[1].event
    assert second_call_event["transcripts"] == ["first", "second"]
    popped = queue.pop_if_fresh()
    assert popped is not None
    assert popped.text == "finally responded"


@pytest.mark.asyncio
async def test_cancel_in_flight_no_op_when_idle() -> None:
    router, _, _, _, _, _, _ = _make_router()
    # Should not raise.
    await router.cancel_in_flight()


@pytest.mark.asyncio
async def test_invariant_i3_t_ms_monotonic_across_dispatches() -> None:
    """Two sequential dispatches → second snapshot t_ms strictly greater."""
    clock = _FakeClock(t0_ms=1_000)
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("a")
    brain.enqueue_stay_silent("b")
    router, _, _, snapshots, _, _, snap_tasks = _make_router(brain=brain, clock=clock)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=900, payload={"text": "a"}))
    await _await_loop(router)
    clock.advance(50)
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_010, payload={"text": "b"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(snapshots.posts) == 2
    assert snapshots.posts[1].t_ms > snapshots.posts[0].t_ms


@pytest.mark.asyncio
async def test_brain_authentication_error_does_not_wedge_router() -> None:
    brain = FakeBrainClient(
        raise_on_call=anthropic.AuthenticationError(
            message="bad key",
            response=_build_404_response(401),
            body=None,
        )
    )
    router, _, _, _, _, _, _ = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    # The dispatch loop catches the unhandled AuthenticationError via
    # the `except Exception` backstop, logs, and stays alive.
    await _await_loop(router)

    assert router._dispatching is False
    assert router._in_flight is None
    # Subsequent calls still dispatch; brain raises again, still survives.
    brain.raise_on_call = None
    brain.enqueue_stay_silent("recovered")
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=2_000, payload={"text": "y"}))
    await _await_loop(router)
    assert len(brain.calls) == 2


@pytest.mark.asyncio
async def test_cas_exhausted_still_posts_snapshot_and_pushes_utterance() -> None:
    store = FakeSessionStore()
    store.cas_error = RedisCasExhaustedError("simulated contention")
    brain = FakeBrainClient()
    brain.enqueue_speak("speak even on cas")
    router, _, _, snapshots, queue, _, snap_tasks = _make_router(brain=brain, store=store)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # Snapshot is still written (with pre-apply state).
    assert len(snapshots.posts) == 1
    # Utterance still queued — silence on CAS contention is worse than
    # losing one state delta.
    popped = queue.pop_if_fresh()
    assert popped is not None
    assert popped.text == "speak even on cas"


@pytest.mark.asyncio
async def test_stay_silent_does_not_push_utterance() -> None:
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("nothing to say")
    router, _, _, snapshots, queue, _, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert queue.pop_if_fresh() is None
    assert len(snapshots.posts) == 1


@pytest.mark.asyncio
async def test_low_confidence_speak_abstains() -> None:
    brain = FakeBrainClient()
    brain.enqueue_speak("uncertain", confidence=0.55)
    router, _, _, snapshots, queue, _, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert queue.pop_if_fresh() is None
    # Snapshot still written so the abstain moment is observable.
    assert len(snapshots.posts) == 1


@pytest.mark.asyncio
async def test_canvas_change_rejected_at_handle_entry() -> None:
    router, brain, _, _, _, _, _ = _make_router()
    with pytest.raises(NotImplementedError, match="canvas_change"):
        await router.handle(RouterEvent(EventType.CANVAS_CHANGE, t_ms=100))
    assert router._dispatching is False
    assert len(router._pending) == 0
    assert len(brain.calls) == 0


@pytest.mark.asyncio
async def test_cost_guard_short_circuits_when_over_cap() -> None:
    state = _make_state(cost_usd_total=5.01, cost_cap_usd=5.0)
    router, brain, _, snapshots, queue, log_events, snap_tasks = _make_router(
        seed_state=state,
    )
    brain.enqueue_speak("would speak if cap not hit")

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # Brain client never called — cost guard short-circuited.
    assert len(brain.calls) == 0
    assert router._cost_capped is True
    # Snapshot + ledger still written for observability.
    assert len(snapshots.posts) == 1
    decision_events = [e for e in log_events if e[0] == "brain_decision"]
    assert decision_events[0][1]["reason"] == "cost_capped"
    # No utterance pushed.
    assert queue.pop_if_fresh() is None


@pytest.mark.asyncio
async def test_cost_capped_persists_across_subsequent_dispatches() -> None:
    state = _make_state(cost_usd_total=5.01, cost_cap_usd=5.0)
    router, brain, _, _, _, _, snap_tasks = _make_router(seed_state=state)

    for t in (1_000, 1_100, 1_200):
        await router.handle(RouterEvent(EventType.TURN_END, t_ms=t, payload={"text": "x"}))
        await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 0


@pytest.mark.asyncio
async def test_schema_violation_counter_escalates_on_third() -> None:
    brain = FakeBrainClient()
    for _ in range(4):
        brain.enqueue_schema_violation()
    router, _, _, _, _, log_events, snap_tasks = _make_router(brain=brain)

    for t in (1_000, 1_100, 1_200, 1_300):
        await router.handle(RouterEvent(EventType.TURN_END, t_ms=t, payload={"text": "x"}))
        await _await_loop(router)
    await _await_snapshots(snap_tasks)

    escalated = [
        e
        for e in log_events
        if e[0] == "brain_decision" and e[1].get("reason") == "schema_violation_escalated"
    ]
    assert len(escalated) == 1  # exactly once


@pytest.mark.asyncio
async def test_schema_violation_counter_resets_on_valid_decision() -> None:
    brain = FakeBrainClient()
    brain.enqueue_schema_violation()
    brain.enqueue_schema_violation()
    brain.enqueue_speak("recovered", confidence=0.9)
    brain.enqueue_schema_violation()
    router, _, _, _, _, log_events, snap_tasks = _make_router(brain=brain)

    for t in (1_000, 1_100, 1_200, 1_300):
        await router.handle(RouterEvent(EventType.TURN_END, t_ms=t, payload={"text": "x"}))
        await _await_loop(router)
    await _await_snapshots(snap_tasks)

    escalated = [
        e
        for e in log_events
        if e[0] == "brain_decision" and e[1].get("reason") == "schema_violation_escalated"
    ]
    assert len(escalated) == 0  # counter reset; never reached 3


@pytest.mark.asyncio
async def test_drain_drops_pending_and_finishes_in_flight() -> None:
    brain = FakeBrainClient(delay_s=0.05)
    brain.enqueue_speak("in flight")
    router, _, _, snapshots, _, _, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "a"}))
    # Yield so the dispatch task enters `await brain.decide(0.05s)`.
    await asyncio.sleep(0)
    # Pile two more events into pending while the first is in flight.
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_100, payload={"text": "b"}))
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_200, payload={"text": "c"}))

    await router.drain()
    await _await_snapshots(snap_tasks)

    assert len(router._pending) == 0
    # First brain call completes; pending dropped → only 1 brain call total.
    assert len(brain.calls) == 1
    assert len(snapshots.posts) == 1


@pytest.mark.asyncio
async def test_coalesced_batch_writes_single_snapshot() -> None:
    brain = FakeBrainClient(delay_s=0.03)
    brain.enqueue_speak("first")
    brain.enqueue_speak("merged")
    router, _, _, snapshots, _, _, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "a"}))
    await asyncio.sleep(0)  # let the first dispatch enter brain.decide
    await router.handle(RouterEvent(EventType.LONG_SILENCE, t_ms=1_100))
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_200, payload={"text": "b"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # First call: single turn_end. Second call: coalesced long_silence + turn_end.
    assert len(brain.calls) == 2
    assert len(snapshots.posts) == 2
    second_event = snapshots.posts[1].event_payload_json
    assert second_event["type"] == "turn_end"
    assert second_event["transcripts"] == ["b"]


@pytest.mark.asyncio
async def test_state_updates_persist_to_store() -> None:
    """A `state_updates` dict from the brain rolls into the next state.

    The tool schema's sub-keys (``phase_advance``, ``new_decision``,
    ``session_summary_append``…) don't match ``SessionState`` field
    names 1:1 — the router routes them through
    ``SessionState.with_state_updates`` which translates + validates.
    This test locks in that a realistic brain payload actually lands
    in the real fields (prior to the translator, this silently dropped
    every update).
    """
    brain = FakeBrainClient()
    brain.enqueue(
        BrainDecision(
            decision="update_only",
            priority="low",
            confidence=0.9,
            reasoning="track summary",
            state_updates={
                "phase_advance": "requirements",
                "session_summary_append": "candidate clarified scope",
                "new_decision": {
                    "t_ms": 42000,
                    "decision": "Use consistent hashing",
                    "reasoning": "even distribution across shards",
                    "alternatives": ["mod-n"],
                },
            },
            usage=BrainUsage(input_tokens=10, output_tokens=2, cost_usd=0.001),
        )
    )
    router, _, store, _, _, _, snap_tasks = _make_router(brain=brain)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    state_after = store._states[SESSION_ID]
    assert state_after.phase.value == "requirements"
    assert state_after.session_summary == "candidate clarified scope"
    assert len(state_after.decisions) == 1
    assert state_after.decisions[0].decision == "Use consistent hashing"
    assert state_after.cost_usd_total == pytest.approx(0.001)
    assert state_after.tokens_input_total == 10
    assert state_after.tokens_output_total == 2


def _build_404_response(status: int) -> Any:
    """Build a minimal `httpx.Response` for AnthropicError construction."""
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status, request=request)


# Silence noisy structlog ERRORs from intentional brain.unexpected logs.
logging.getLogger("archmentor_agent.events.router").setLevel(logging.CRITICAL)
