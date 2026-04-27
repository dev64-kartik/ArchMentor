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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import anthropic
import pytest
from _helpers import FakeBrainClient, FakeSessionStore, FakeSnapshotClient
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision, BrainUsage
from archmentor_agent.events import EventRouter, EventType, RouterEvent
from archmentor_agent.queue import UtteranceQueue
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
    log_events: list[tuple[str, dict[str, Any]]] | None = None,
    seed_state: SessionState | None = None,
    recovery_text: str = "",
    streaming_tts_factory: Any = None,
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
    # `UtteranceQueue.__len__` makes empty queues falsy — use `is None`
    # so a caller-passed empty queue isn't silently replaced.
    if queue is None:
        queue = UtteranceQueue(clock.now)
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
        log_event=log,
        now_ms=clock.now,
        recovery_text=recovery_text,
        streaming_tts_factory=streaming_tts_factory,
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
async def test_canvas_change_dispatches_through_router() -> None:
    """M3: canvas_change events flow through the dispatcher (no NotImplementedError)."""
    brain = FakeBrainClient()
    brain.enqueue_speak("That box is mislabeled.", confidence=0.85)
    router, _, _, snapshots, _queue, log_events, snap_tasks = _make_router(brain=brain)

    from archmentor_agent.events.types import Priority

    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=2_000,
            payload={"scene_text": "Components: <label>API</label>"},
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 1
    assert brain.calls[0].event["type"] == "canvas_change"
    assert len(snapshots.posts) == 1
    decisions = [e for e in log_events if e[0] == "brain_decision"]
    assert len(decisions) == 1


_TEST_RECOVERY_TEXT = "Let me come back to that — please continue."


@pytest.mark.asyncio
async def test_synthetic_recovery_fires_once_on_brain_timeout() -> None:
    """R27: brain_timeout decision fires the synthetic recovery utterance
    exactly once per session."""
    brain = FakeBrainClient()
    # Enqueue two stay_silent decisions whose reason marks them as
    # brain_timeout — Unit 12 wraps the live client to actually emit
    # this reason; the router-side wiring under test is the same.
    for _ in range(2):
        brain.enqueue_stay_silent("brain_timeout")
    emissions: list[tuple[str, str]] = []

    def emit(*, text: str, reason: str) -> None:
        emissions.append((text, reason))

    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain, recovery_text=_TEST_RECOVERY_TEXT)
    router._emit_synthetic = emit  # inject post-construction; mirrors entrypoint wiring

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "a"}))
    await _await_loop(router)
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=2_000, payload={"text": "b"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(emissions) == 1
    text, reason = emissions[0]
    assert reason == "brain_timeout"
    assert text == _TEST_RECOVERY_TEXT
    assert router._apology_used is True


@pytest.mark.asyncio
async def test_synthetic_recovery_skipped_when_no_emitter_wired() -> None:
    """Tests / kill-switch paths construct routers without an emitter.
    The router must stay callable; the apology branch must no-op."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("brain_timeout")
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain)
    assert router._emit_synthetic is None

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)
    # No crash, no apology issued.
    assert router._apology_used is False


@pytest.mark.asyncio
async def test_synthetic_recovery_not_fired_for_other_reasons() -> None:
    """Only `brain_timeout` and `anthropic_api_connection_during_wait_for`
    trigger R27 — other stay_silent reasons (api_error, schema_violation,
    cost_capped) do not."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("api_error")
    brain.enqueue_stay_silent("low_confidence")
    emissions: list[tuple[str, str]] = []

    def emit(*, text: str, reason: str) -> None:
        emissions.append((text, reason))

    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain)
    router._emit_synthetic = emit

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "a"}))
    await _await_loop(router)
    await router.handle(RouterEvent(EventType.TURN_END, t_ms=2_000, payload={"text": "b"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert emissions == []


@pytest.mark.asyncio
async def test_synthetic_recovery_fires_on_anthropic_api_connection_during_wait_for() -> None:
    """R27 must also fire when the brain client returns
    `reason="anthropic_api_connection_during_wait_for"` (Fix 5 — SDK converts
    wait_for CancelledError → APIConnectionError mid-backoff after deadline)."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("anthropic_api_connection_during_wait_for")
    emissions: list[tuple[str, str]] = []

    def emit(*, text: str, reason: str) -> None:
        emissions.append((text, reason))

    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain, recovery_text=_TEST_RECOVERY_TEXT)
    router._emit_synthetic = emit

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "a"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(emissions) == 1
    text, reason = emissions[0]
    assert text == _TEST_RECOVERY_TEXT
    assert reason == "anthropic_api_connection_during_wait_for"
    assert router._apology_used is True


@pytest.mark.asyncio
async def test_cost_capped_on_canvas_change_still_emits_snapshot_and_decision() -> None:
    """R22: HIGH priority does NOT bypass the cost cap. Canvas events on a
    capped session produce no Anthropic call but still snapshot + ledger."""
    state = _make_state(cost_usd_total=5.01, cost_cap_usd=5.0)
    brain = FakeBrainClient()
    # Even though we enqueue a decision, the cost guard short-circuits
    # before the brain is called — so this entry is never consumed.
    brain.enqueue_speak("would not run")
    router, _, _, snapshots, _, log_events, snap_tasks = _make_router(brain=brain, seed_state=state)

    from archmentor_agent.events.types import Priority

    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=2_000,
            payload={"scene_text": "Components: <label>API</label>"},
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 0
    assert router._cost_capped is True
    assert len(snapshots.posts) == 1
    decisions = [e for e in log_events if e[0] == "brain_decision"]
    assert decisions[0][1]["reason"] == "cost_capped"


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


# ---------------------------------------------------------------------------
# Unit 1 — Cost-throttle: fingerprint skip + exponential backoff
# (R20, R21; refinements R1, R2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_idempotent_skips_second_canvas_change() -> None:
    """Two identical CANVAS_CHANGE events back-to-back → second short-circuits.

    First call dispatches and gets ``stay_silent``; second skips with
    ``BrainDecision.skipped_idempotent()``, no Anthropic call, snapshot
    + ``brain_decision`` ledger row still emit, and BrainUsage is empty.
    """
    from archmentor_agent.events.types import Priority

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("nothing to add")
    router, _, _, snapshots, queue, log_events, snap_tasks = _make_router(brain=brain)

    payload = {"scene_text": "Components: <label>API</label>"}
    for t in (1_000, 1_100):
        await router.handle(
            RouterEvent(
                EventType.CANVAS_CHANGE,
                t_ms=t,
                payload=dict(payload),
                priority=Priority.HIGH,
            )
        )
        await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # Brain called exactly once (the second was skipped).
    assert len(brain.calls) == 1
    # Two snapshots posted (skipped path still emits for replay).
    assert len(snapshots.posts) == 2
    # Two brain_decision ledger rows; second has reason=skipped_idempotent.
    decisions = [e for e in log_events if e[0] == "brain_decision"]
    assert len(decisions) == 2
    assert decisions[1][1]["reason"] == "skipped_idempotent"
    # No utterance pushed on either.
    assert queue.pop_if_fresh() is None


@pytest.mark.asyncio
async def test_fingerprint_skip_does_not_apply_to_turn_end() -> None:
    """Identical CANVAS_CHANGE events sandwiching a TURN_END → both dispatch.

    TURN_END resets the fingerprint cache to its own value, so the
    second CANVAS_CHANGE does not match the first's fingerprint.
    """
    from archmentor_agent.events.types import Priority

    brain = FakeBrainClient()
    for _ in range(3):
        brain.enqueue_stay_silent("ack")
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain)

    canvas_payload = {"scene_text": "<label>Cache</label>"}
    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=1_000,
            payload=dict(canvas_payload),
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)
    await router.handle(
        RouterEvent(EventType.TURN_END, t_ms=2_000, payload={"text": "hello"}),
    )
    await _await_loop(router)
    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=3_000,
            payload=dict(canvas_payload),
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # All three dispatched (no skipped_idempotent in the chain).
    assert len(brain.calls) == 3


@pytest.mark.asyncio
async def test_cooldown_short_circuits_after_two_stay_silent() -> None:
    """Two stay_silent outcomes from non-skipped Opus calls →
    next CANVAS_CHANGE within 8 s short-circuits to skipped_cooldown."""
    from archmentor_agent.events.types import Priority

    clock = _FakeClock(t0_ms=1_000)
    brain = FakeBrainClient()
    # Two distinct stay_silent calls (different payloads → different
    # fingerprints) so each makes a real call and increments the counter.
    brain.enqueue_stay_silent("a")
    brain.enqueue_stay_silent("b")
    router, _, _, _, _, log_events, snap_tasks = _make_router(brain=brain, clock=clock)

    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=clock.now(),
            payload={"scene_text": "v1"},
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)

    clock.advance(2_000)
    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=clock.now(),
            payload={"scene_text": "v2"},
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)

    # Counter is now 2; cooldown of 8000ms is set from t=3000 → t=11000.
    assert router._consecutive_stay_silent == 2
    assert router._cooldown_until_ms == clock.now() + 8_000

    # A CANVAS_CHANGE within the cooldown window is short-circuited.
    clock.advance(1_000)
    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=clock.now(),
            payload={"scene_text": "v3"},
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)

    assert len(brain.calls) == 2  # third was skipped_cooldown
    decisions = [e for e in log_events if e[0] == "brain_decision"]
    assert decisions[-1][1]["reason"] == "stay_silent_backoff"
    await _await_snapshots(snap_tasks)


@pytest.mark.asyncio
async def test_cooldown_clamps_at_60_seconds() -> None:
    """At N=10, cooldown_ms clamps at 60_000, not 4_000 * 2^9 = 2_048_000."""
    router, _, _, _, _, _, _ = _make_router()
    router._consecutive_stay_silent = 10
    assert router._active_cooldown_ms() == 60_000


@pytest.mark.asyncio
async def test_cost_capped_runs_before_throttle_gates() -> None:
    """Cost-capped session → cost_capped fires; fingerprint/cooldown
    paths are bypassed entirely."""
    state = _make_state(cost_usd_total=5.01, cost_cap_usd=5.0)
    router, brain, _, _snapshots, _, log_events, snap_tasks = _make_router(
        seed_state=state,
    )
    brain.enqueue_speak("would not run")

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 0
    assert router._cost_capped is True
    decisions = [e for e in log_events if e[0] == "brain_decision"]
    assert decisions[0][1]["reason"] == "cost_capped"


@pytest.mark.asyncio
async def test_consecutive_stay_silent_resets_on_speak() -> None:
    """A speak decision after two stay_silent resets the counter to 0."""
    clock = _FakeClock(t0_ms=1_000)
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("first")
    brain.enqueue_stay_silent("second")
    brain.enqueue_speak("now I have something", confidence=0.9)
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain, clock=clock)

    for label, t_offset in (("a", 0), ("b", 2_000), ("c", 4_000)):
        clock.advance(t_offset)
        await router.handle(
            RouterEvent(EventType.TURN_END, t_ms=clock.now(), payload={"text": label}),
        )
        await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert router._consecutive_stay_silent == 0
    assert router._cooldown_until_ms == 0


@pytest.mark.asyncio
async def test_skipped_idempotent_does_not_count_toward_cooldown() -> None:
    """Two skipped_idempotent paths must not arm the backoff cooldown.

    Otherwise the throttle eats its own tail — backoff produces more
    skips which extend backoff.
    """
    from archmentor_agent.events.types import Priority

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("seed")  # only one real call expected
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain)

    payload = {"scene_text": "stable scene"}
    for t in (1_000, 1_100, 1_200):
        await router.handle(
            RouterEvent(
                EventType.CANVAS_CHANGE,
                t_ms=t,
                payload=dict(payload),
                priority=Priority.HIGH,
            )
        )
        await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 1
    # One real stay_silent → counter=1; two skipped_idempotents → still 1.
    assert router._consecutive_stay_silent == 1
    assert router._cooldown_until_ms == 0


@pytest.mark.asyncio
async def test_phase_timer_bypasses_cooldown() -> None:
    """Refinements R2: a PHASE_TIMER fired during an active cooldown
    dispatches normally — it exists *to break a stuck silence*."""
    from archmentor_agent.events.types import Priority

    clock = _FakeClock(t0_ms=1_000)
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("a")
    brain.enqueue_stay_silent("b")
    brain.enqueue_speak("we should advance phase", confidence=0.9)
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain, clock=clock)

    # Two CANVAS_CHANGEs to arm the cooldown.
    for v in ("v1", "v2"):
        clock.advance(1_000)
        await router.handle(
            RouterEvent(
                EventType.CANVAS_CHANGE,
                t_ms=clock.now(),
                payload={"scene_text": v},
                priority=Priority.HIGH,
            )
        )
        await _await_loop(router)

    assert router._consecutive_stay_silent == 2
    cooldown_until = router._cooldown_until_ms
    assert cooldown_until > clock.now()  # cooldown is active

    # PHASE_TIMER inside the cooldown window dispatches anyway.
    clock.advance(500)
    await router.handle(
        RouterEvent(
            EventType.PHASE_TIMER,
            t_ms=clock.now(),
            payload={"phase": "requirements", "over_budget_pct_tier": 50},
            priority=Priority.LOW,
        )
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # Three real brain calls: two CANVAS_CHANGE + the PHASE_TIMER bypass.
    assert len(brain.calls) == 3


@pytest.mark.asyncio
async def test_phase_timer_does_not_reset_consecutive_stay_silent() -> None:
    """Refinements R2: an over-budget phase silence shouldn't mask a
    stuck-silence state. PHASE_TIMER does NOT reset the counter."""
    from archmentor_agent.events.types import Priority

    clock = _FakeClock(t0_ms=1_000)
    brain = FakeBrainClient()
    # First CANVAS_CHANGE → stay_silent (n=1). PHASE_TIMER → also stay_silent.
    # Expect counter = 2 after PHASE_TIMER, not 1.
    brain.enqueue_stay_silent("a")
    brain.enqueue_stay_silent("b")
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain, clock=clock)

    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=clock.now(),
            payload={"scene_text": "v1"},
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)
    assert router._consecutive_stay_silent == 1

    clock.advance(1_000)
    await router.handle(
        RouterEvent(
            EventType.PHASE_TIMER,
            t_ms=clock.now(),
            payload={"phase": "requirements", "over_budget_pct_tier": 50},
            priority=Priority.LOW,
        )
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # PHASE_TIMER must NOT reset the counter. Real stay_silent →
    # counter increments to 2.
    assert router._consecutive_stay_silent == 2


@pytest.mark.asyncio
async def test_phase_timer_subject_to_fingerprint_skip() -> None:
    """Refinements R2: PHASE_TIMER passes through the fingerprint-skip gate.

    Two PHASE_TIMER events with identical bucketed ``over_budget_pct_tier``
    short-circuit to skipped_idempotent — Unit 7's bucketing keeps the
    payload stable within a tier so cost stays bounded during stuck-silence.
    """
    from archmentor_agent.events.types import Priority

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("phase ack")
    router, _, _, _, _, log_events, snap_tasks = _make_router(brain=brain)

    payload = {"phase": "requirements", "over_budget_pct_tier": 50}
    for t in (1_000, 1_100):
        await router.handle(
            RouterEvent(
                EventType.PHASE_TIMER,
                t_ms=t,
                payload=dict(payload),
                priority=Priority.LOW,
            )
        )
        await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 1
    decisions = [e for e in log_events if e[0] == "brain_decision"]
    assert decisions[-1][1]["reason"] == "skipped_idempotent"


@pytest.mark.asyncio
async def test_fingerprint_excludes_irrelevant_state_fields() -> None:
    """Two states differing only in ``cost_usd_total``, ``tokens_*_total``,
    ``session_summary``, ``canvas_state.description``, and ``pending_utterance``
    must produce the same fingerprint.

    Refinements R1: those fields are not in the brain's decision surface
    relative to whether to speak; they should not flip the throttle hash.
    """
    from archmentor_agent.events.types import Priority

    state_a = _make_state()
    state_b = state_a.model_copy(
        update={
            "cost_usd_total": 1.234,
            "tokens_input_total": 500,
            "tokens_output_total": 200,
            "session_summary": "A long compaction summary added by Haiku.",
            "canvas_state": state_a.canvas_state.model_copy(
                update={"description": "[label=API]"},
            ),
        }
    )

    router, _, _, _, _, _, _ = _make_router()
    event = RouterEvent(
        EventType.CANVAS_CHANGE,
        t_ms=1_000,
        payload={"scene_text": "stable"},
        priority=Priority.HIGH,
    )
    fp_a = router._compute_fingerprint(state_a, event)
    fp_b = router._compute_fingerprint(state_b, event)
    assert fp_a == fp_b


@pytest.mark.asyncio
async def test_fingerprint_excludes_t_ms_and_merged_from() -> None:
    """Two events differing only in ``t_ms`` and ``merged_from`` produce
    the same fingerprint."""
    from archmentor_agent.events.types import Priority

    state = _make_state()
    router, _, _, _, _, _, _ = _make_router(seed_state=state)

    event_a = RouterEvent(
        EventType.CANVAS_CHANGE,
        t_ms=1_000,
        payload={"scene_text": "x", "merged_from": ["canvas_change"]},
        priority=Priority.HIGH,
    )
    event_b = RouterEvent(
        EventType.CANVAS_CHANGE,
        t_ms=9_999_999,
        payload={"scene_text": "x", "merged_from": ["canvas_change", "turn_end"]},
        priority=Priority.HIGH,
    )
    assert router._compute_fingerprint(state, event_a) == router._compute_fingerprint(
        state, event_b
    )


@pytest.mark.asyncio
async def test_fingerprint_concurrent_transcripts_order_invariant() -> None:
    """``concurrent_transcripts`` arrival order must not flicker the hash."""
    from archmentor_agent.events.types import Priority

    state = _make_state()
    router, _, _, _, _, _, _ = _make_router(seed_state=state)

    event_a = RouterEvent(
        EventType.CANVAS_CHANGE,
        t_ms=1_000,
        payload={"scene_text": "x", "concurrent_transcripts": ["alpha", "beta"]},
        priority=Priority.HIGH,
    )
    event_b = RouterEvent(
        EventType.CANVAS_CHANGE,
        t_ms=1_000,
        payload={"scene_text": "x", "concurrent_transcripts": ["beta", "alpha"]},
        priority=Priority.HIGH,
    )
    assert router._compute_fingerprint(state, event_a) == router._compute_fingerprint(
        state, event_b
    )


# ---------------------------------------------------------------------------
# Unit 2 — Queue-drain prioritisation + freshness-aware TTL (R22, R23)
# ---------------------------------------------------------------------------


def _make_router_with_callback(
    callback: Callable[[], Awaitable[None]] | None,
    *,
    brain: FakeBrainClient | None = None,
    clock: _FakeClock | None = None,
    queue: UtteranceQueue | None = None,
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
    """Variant of `_make_router` that passes a pre-dispatch callback."""
    brain = brain or FakeBrainClient()
    store = FakeSessionStore()
    snapshots = FakeSnapshotClient()
    clock = clock or _FakeClock()
    # `UtteranceQueue` defines ``__len__`` so the empty queue evaluates
    # as falsy — `queue or UtteranceQueue(...)` would silently create a
    # new instance and break identity. Use `is None` instead.
    if queue is None:
        queue = UtteranceQueue(clock.now)
    log_events: list[tuple[str, dict[str, Any]]] = []

    state = seed_state if seed_state is not None else _make_state()
    asyncio.get_event_loop()
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
        log_event=log,
        now_ms=clock.now,
        pre_dispatch_callback=callback,
    )
    return router, brain, store, snapshots, queue, log_events, snapshot_tasks


@pytest.mark.asyncio
async def test_pre_dispatch_callback_drains_queued_utterance_before_next_call() -> None:
    """A queued speak from a prior dispatch is delivered before the
    next brain call starts (master plan §697 lever (a))."""
    from archmentor_agent.events.types import Priority
    from archmentor_agent.state.session_state import PendingUtterance

    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now)
    drained: list[PendingUtterance] = []

    async def drain() -> None:
        item = queue.pop_if_fresh()
        if item is not None:
            drained.append(item)

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("post-canvas")
    router, _, _, _, _, _, snap_tasks = _make_router_with_callback(
        drain, brain=brain, clock=clock, queue=queue
    )

    # Pre-seed the queue with a fresh utterance from a "prior" dispatch.
    queue.push(
        PendingUtterance(text="hello from prior", generated_at_ms=clock.now(), ttl_ms=10_000)
    )

    # Now fire a CANVAS_CHANGE — the router must drain the queue first.
    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=clock.now() + 50,
            payload={"scene_text": "x"},
            priority=Priority.HIGH,
        )
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(drained) == 1
    assert drained[0].text == "hello from prior"
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_pre_dispatch_callback_skipped_when_queue_empty() -> None:
    """No callback invocation when peek_fresh returns None — cheap path."""
    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now)

    invocations = 0

    async def drain() -> None:
        nonlocal invocations
        invocations += 1

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("nothing queued")
    router, _, _, _, _, _, snap_tasks = _make_router_with_callback(
        drain, brain=brain, clock=clock, queue=queue
    )

    await router.handle(
        RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}),
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert invocations == 0


@pytest.mark.asyncio
async def test_pre_dispatch_callback_none_preserves_m3_behaviour() -> None:
    """Kill-switch / test path with no callback registered: dispatch
    proceeds; no crash; no drain."""
    from archmentor_agent.state.session_state import PendingUtterance

    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now)
    queue.push(
        PendingUtterance(text="orphan", generated_at_ms=clock.now(), ttl_ms=10_000),
    )

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("no callback")
    router, _, _, _, _, _, snap_tasks = _make_router_with_callback(
        None, brain=brain, clock=clock, queue=queue
    )

    await router.handle(
        RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}),
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # Brain still called; orphan utterance still in queue (no drain).
    assert len(brain.calls) == 1
    assert len(queue) == 1


@pytest.mark.asyncio
async def test_pre_dispatch_callback_error_does_not_kill_dispatch_loop() -> None:
    """Errors inside the pre-dispatch callback are logged but don't
    stop the dispatch — the brain call still runs."""
    from archmentor_agent.events.types import Priority
    from archmentor_agent.state.session_state import PendingUtterance

    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now)
    queue.push(PendingUtterance(text="x", generated_at_ms=clock.now(), ttl_ms=10_000))

    async def boom() -> None:
        raise RuntimeError("intentional callback failure")

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("recovered")
    router, _, _, _, _, _, snap_tasks = _make_router_with_callback(
        boom, brain=brain, clock=clock, queue=queue
    )

    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=clock.now(),
            payload={"scene_text": "x"},
            priority=Priority.HIGH,
        ),
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_ttl_bump_extends_queued_utterance_freshness() -> None:
    """An utterance queued just before a slow brain call must NOT age
    past TTL purely because the call held the dispatch slot
    (master plan §697 lever (b))."""
    from archmentor_agent.state.session_state import PendingUtterance

    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now, ttl_ms=10_000)

    # Push first, simulating a queued speak from a prior dispatch.
    queue.push(PendingUtterance(text="needs to survive", generated_at_ms=clock.now()))

    # Build a brain client that advances the clock during decide() so
    # the dispatch's start→end window covers a 9-second wait.
    class _SlowBrain(FakeBrainClient):
        async def decide(  # type: ignore[override]
            self,
            *,
            state: SessionState,
            event: dict[str, Any],
            t_ms: int,
            utterance_listener: Any = None,
        ) -> Any:
            clock.advance(9_000)
            return await super().decide(
                state=state,
                event=event,
                t_ms=t_ms,
                utterance_listener=utterance_listener,
            )

    brain = _SlowBrain()
    brain.enqueue_stay_silent("slow")
    router, _, _, _, _, _, snap_tasks = _make_router_with_callback(
        None, brain=brain, clock=clock, queue=queue
    )

    await router.handle(
        RouterEvent(EventType.TURN_END, t_ms=clock.now(), payload={"text": "x"}),
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # 9_000 ms have elapsed; without bump the utterance would be 9_000 ms
    # past TTL — but bump set ttl_ms to ~19_000 so it's still fresh.
    peeked = queue.peek_fresh()
    assert peeked is not None
    assert peeked.text == "needs to survive"
    assert peeked.ttl_ms >= 19_000


@pytest.mark.asyncio
async def test_pre_dispatch_callback_fires_on_canvas_after_queued_turn_end_speak() -> None:
    """M3-dogfood reproducer (i): TURN_END speak queued; subsequent
    CANVAS_CHANGE dispatch chain → speak delivered, not dropped.

    Pre-Unit 2, the queued TURN_END speak aged past TTL during the
    CANVAS_CHANGE dispatch and was dropped on the next pop_if_fresh.
    """
    from archmentor_agent.events.types import Priority
    from archmentor_agent.state.session_state import PendingUtterance

    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now)

    drained: list[PendingUtterance] = []

    async def drain() -> None:
        item = queue.pop_if_fresh()
        if item is not None:
            drained.append(item)

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("canvas-only ack")
    router, _, _, _, _, _, snap_tasks = _make_router_with_callback(
        drain, brain=brain, clock=clock, queue=queue
    )

    # Simulate a TURN_END dispatch having pushed a fresh speak; then
    # 12s later a CANVAS_CHANGE arrives — pre-Unit 2 the speak was lost.
    queue.push(PendingUtterance(text="from turn_end", generated_at_ms=clock.now(), ttl_ms=10_000))
    clock.advance(2_000)  # leaving 8s of life
    await router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=clock.now(),
            payload={"scene_text": "drawing"},
            priority=Priority.HIGH,
        ),
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(drained) == 1
    assert drained[0].text == "from turn_end"


@pytest.mark.asyncio
async def test_pre_dispatch_callback_fires_on_turn_end_after_queued_canvas_speak() -> None:
    """M3-dogfood reproducer (ii): CANVAS_CHANGE speak queued; following
    TURN_END dispatch → canvas speak delivered, not dropped."""
    from archmentor_agent.state.session_state import PendingUtterance

    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now)

    drained: list[PendingUtterance] = []

    async def drain() -> None:
        item = queue.pop_if_fresh()
        if item is not None:
            drained.append(item)

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("turn_end ack")
    router, _, _, _, _, _, snap_tasks = _make_router_with_callback(
        drain, brain=brain, clock=clock, queue=queue
    )

    queue.push(PendingUtterance(text="from canvas", generated_at_ms=clock.now(), ttl_ms=10_000))
    clock.advance(3_000)
    await router.handle(
        RouterEvent(EventType.TURN_END, t_ms=clock.now(), payload={"text": "candidate"}),
    )
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(drained) == 1
    assert drained[0].text == "from canvas"


# Property test (binding, refinements R1) — fingerprint stable across
# `session_summary` mutation that does NOT change `transcript_turn_count`.
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


@settings(max_examples=50, deadline=None)
@given(
    summary_a=st.text(min_size=0, max_size=200),
    summary_b=st.text(min_size=0, max_size=200),
)
def test_fingerprint_stable_across_session_summary_mutation(summary_a: str, summary_b: str) -> None:
    """Compaction's parallel ``session_summary`` mutation must not flip
    the fingerprint when ``transcript_turn_count`` is unchanged.

    Without this invariant, the cost throttle silently degrades to
    no-op whenever the Haiku compactor appends to ``session_summary``
    between two otherwise-identical brain inputs (per refinements R1).
    """
    from archmentor_agent.events.types import Priority

    base_state = _make_state()
    state_a = base_state.model_copy(update={"session_summary": summary_a})
    state_b = base_state.model_copy(update={"session_summary": summary_b})

    # Construct a router synchronously — `_make_router` requires an
    # event loop, but `_compute_fingerprint` does not, so build the
    # bare collaborator graph inline.
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    try:
        _asyncio.set_event_loop(loop)
        router, _, _, _, _, _, _ = _make_router(seed_state=base_state)
        event = RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=1_000,
            payload={"scene_text": "stable"},
            priority=Priority.HIGH,
        )
        fp_a = router._compute_fingerprint(state_a, event)
        fp_b = router._compute_fingerprint(state_b, event)
        assert fp_a == fp_b
    finally:
        loop.close()


@settings(max_examples=30, deadline=None)
@given(turn_count_a=st.integers(min_value=0, max_value=200))
def test_fingerprint_changes_with_transcript_turn_count(turn_count_a: int) -> None:
    """Compaction's transcript-window decrement DOES flip the fingerprint
    (correct behaviour — brain reads compressed summary plus a smaller
    window). Different turn counts → different hashes."""
    from archmentor_agent.events.types import Priority
    from archmentor_agent.state.session_state import TranscriptTurn

    base_state = _make_state()

    def _state_with_turn_count(n: int) -> SessionState:
        return base_state.model_copy(
            update={
                "transcript_window": [
                    TranscriptTurn(t_ms=i * 1_000, speaker="candidate", text="x") for i in range(n)
                ],
            },
        )

    state_a = _state_with_turn_count(turn_count_a)
    state_b = _state_with_turn_count(turn_count_a + 1)

    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    try:
        _asyncio.set_event_loop(loop)
        router, _, _, _, _, _, _ = _make_router(seed_state=base_state)
        event = RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=1_000,
            payload={"scene_text": "stable"},
            priority=Priority.HIGH,
        )
        fp_a = router._compute_fingerprint(state_a, event)
        fp_b = router._compute_fingerprint(state_b, event)
        assert fp_a != fp_b
    finally:
        loop.close()


# ─────────────────── M4 streaming TTS factory wiring ─────────────────


class _FakeStreamingTtsHandle:
    """Records every delta the listener received and exposes
    `audio_played` so router tests can assert the queue-skip path."""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.closed = False

    async def listener(self, delta: str) -> None:
        self.deltas.append(delta)

    @property
    def audio_played(self) -> bool:
        return bool(self.deltas)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_streaming_factory_skips_queue_push_on_speak() -> None:
    """When a streaming factory is wired and the brain returns `speak`,
    the listener fires (audio plays live) and the queue is NOT pushed —
    the candidate already heard the utterance."""
    handles: list[_FakeStreamingTtsHandle] = []

    def factory() -> _FakeStreamingTtsHandle:
        h = _FakeStreamingTtsHandle()
        handles.append(h)
        return h

    brain = FakeBrainClient()
    brain.enqueue_speak("Walk me through capacity.", confidence=0.9)
    router, _, _, _, queue, _, snap_tasks = _make_router(brain=brain, streaming_tts_factory=factory)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(handles) == 1
    assert handles[0].audio_played is True
    assert handles[0].closed is True
    # Queue did NOT get the utterance — streaming consumed it.
    assert len(queue) == 0


@pytest.mark.asyncio
async def test_streaming_factory_queue_push_preserved_on_stay_silent() -> None:
    """`stay_silent` decisions don't invoke the listener; `audio_played`
    is False; legacy queue-push semantics are unaffected (queue stays
    empty either way for stay_silent — the unit test confirms the
    listener was not called and the handle still closed cleanly)."""
    handles: list[_FakeStreamingTtsHandle] = []

    def factory() -> _FakeStreamingTtsHandle:
        h = _FakeStreamingTtsHandle()
        handles.append(h)
        return h

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("stay")
    router, _, _, _, queue, _, snap_tasks = _make_router(brain=brain, streaming_tts_factory=factory)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(handles) == 1
    assert handles[0].audio_played is False
    assert handles[0].closed is True
    assert len(queue) == 0


@pytest.mark.asyncio
async def test_streaming_factory_handle_closed_even_on_brain_error() -> None:
    """If `decide` raises an unexpected exception, the streaming handle
    must still close (router shouldn't leak the SynthesizeStream)."""
    handles: list[_FakeStreamingTtsHandle] = []

    def factory() -> _FakeStreamingTtsHandle:
        h = _FakeStreamingTtsHandle()
        handles.append(h)
        return h

    brain = FakeBrainClient()
    brain.raise_on_call = RuntimeError("boom")
    router, _, _, _, _, _, snap_tasks = _make_router(brain=brain, streaming_tts_factory=factory)

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(handles) == 1
    assert handles[0].closed is True


@pytest.mark.asyncio
async def test_streaming_factory_skipped_paths_dont_open_handle() -> None:
    """Cost-capped / idempotent / cooldown skip paths short-circuit
    BEFORE the brain call — they must not open a streaming TTS handle
    (would leak a fresh SynthesizeStream per skipped tick under heavy
    canvas churn)."""
    handles: list[_FakeStreamingTtsHandle] = []

    def factory() -> _FakeStreamingTtsHandle:
        h = _FakeStreamingTtsHandle()
        handles.append(h)
        return h

    # Seed a state already at the cost cap.
    state = _make_state()
    state = state.model_copy(update={"cost_usd_total": 10.0, "cost_cap_usd": 5.0})
    router, _, _, _, _, _, snap_tasks = _make_router(
        seed_state=state, streaming_tts_factory=factory
    )

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # No factory invocation when the dispatch short-circuits.
    assert handles == []


@pytest.mark.asyncio
async def test_streaming_partial_audio_suppresses_r27() -> None:
    """M4 R3b — when partial audio played during a streaming dispatch
    AND the brain decision was `brain_timeout`, R27 is suppressed (the
    candidate would otherwise hear "Walk me throu— Let me come back to
    that — please continue.")."""

    class _Handle(_FakeStreamingTtsHandle):
        async def listener(self, delta: str) -> None:
            await super().listener(delta)

    fired: list[dict[str, Any]] = []

    def emitter(*, text: str, reason: str) -> None:
        fired.append({"text": text, "reason": reason})

    handle = _Handle()
    # Simulate "the streaming brain emitted some `utterance` deltas,
    # then timed out". We do this by manually marking `audio_played`
    # before the router checks it — easiest way: pre-stuff `deltas`.
    handle.deltas.append("Walk me throu")

    def factory() -> _Handle:
        return handle

    brain = FakeBrainClient()
    brain.enqueue(
        BrainDecision.stay_silent("brain_timeout"),
    )
    router, _, _, _, _, _, snap_tasks = _make_router(
        brain=brain, streaming_tts_factory=factory, recovery_text="recovery"
    )
    router._emit_synthetic = emitter  # type: ignore[assignment]

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    # R27 suppressed because partial audio played.
    assert fired == []
    # `_apology_used` flipped True so future timeouts also don't fire.
    assert router._apology_used is True


@pytest.mark.asyncio
async def test_streaming_no_partial_audio_still_fires_r27() -> None:
    """Inverse of the above — when the brain emits `reasoning` only
    (no utterance), `audio_played` is False and R27 fires normally."""
    handles: list[_FakeStreamingTtsHandle] = []

    def factory() -> _FakeStreamingTtsHandle:
        h = _FakeStreamingTtsHandle()
        handles.append(h)
        return h

    fired: list[dict[str, Any]] = []

    def emitter(*, text: str, reason: str) -> None:
        fired.append({"text": text, "reason": reason})

    brain = FakeBrainClient()
    brain.enqueue(BrainDecision.stay_silent("brain_timeout"))
    router, _, _, _, _, _, snap_tasks = _make_router(
        brain=brain, streaming_tts_factory=factory, recovery_text="recovery"
    )
    router._emit_synthetic = emitter  # type: ignore[assignment]

    await router.handle(RouterEvent(EventType.TURN_END, t_ms=1_000, payload={"text": "x"}))
    await _await_loop(router)
    await _await_snapshots(snap_tasks)

    assert len(fired) == 1
    assert fired[0]["reason"] == "brain_timeout"


# Silence noisy structlog ERRORs from intentional brain.unexpected logs.
logging.getLogger("archmentor_agent.events.router").setLevel(logging.CRITICAL)
