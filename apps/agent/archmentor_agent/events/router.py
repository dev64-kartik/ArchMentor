"""Serialized event router.

Only one brain call in flight at a time. Concurrent events
(`turn_end + long_silence + phase_timer`) coalesce into a single call
with merged context. In-flight calls are cancelled via
`cancel_in_flight()` on candidate-speech resume; their batch is
re-prepended to `pending` so no event is lost.

Invariants the implementation enforces (mirrors the plan's H-L-T-D):

I1. Exactly one `_dispatch` task at a time. The "I own dispatch"
    decision is the `_dispatching` flag flipped under `_lock`, NOT a
    `task.done()` probe — `done()` has a gap between completion and
    the next `create_task` where two callers can both think they
    should dispatch.

I2. `pending` is preserved on cancellation. `_dispatch` keeps the
    coalesced batch in a local; on `CancelledError`, the local batch
    is re-prepended to `pending` so the next `handle(...)` picks it up.

I3. `t_ms` is assigned at dispatch entry, before any `await`. A
    monotonic clock on a single asyncio loop guarantees snapshot rows
    sort correctly even when two snapshot POSTs race in Postgres.

Cost guard lives here, not in `BrainClient` (see plan Key Technical
Decisions). Once `cost_capped` flips True, every subsequent dispatch
short-circuits to a `BrainDecision.cost_capped()` and still emits a
snapshot + ledger row so the cost-cap moment is observable.

Schema-violation escalation: on the 3rd consecutive
`reason=schema_violation`, emit `brain.schema_violation.escalated`
exactly once and a `brain_decision` ledger event with
`reason=schema_violation_escalated`. Counter resets on any successful
(non-violated) decision. The brain is NOT disabled — the next call may
succeed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

import structlog

from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision
from archmentor_agent.events.coalescer import coalesce
from archmentor_agent.events.types import EventType, RouterEvent
from archmentor_agent.queue import SpeechCheckGate, UtteranceQueue
from archmentor_agent.snapshots.client import SnapshotClient
from archmentor_agent.snapshots.serializer import build_snapshot
from archmentor_agent.state.redis_store import (
    RedisCasExhaustedError,
    RedisSessionStore,
)
from archmentor_agent.state.session_state import (
    PendingUtterance,
    SessionState,
)

log = structlog.get_logger(__name__)


_MIN_CONFIDENCE = 0.6
_SCHEMA_VIOLATION_ESCALATION = 3


class LedgerLogger(Protocol):
    """Subset of `MentorAgent._log` the router needs.

    Decoupled via Protocol so the router doesn't import the agent
    module (avoids circular imports) and so tests can pass a list-
    appending fake.
    """

    def __call__(self, event_type: str, payload: dict[str, Any]) -> None: ...


SnapshotScheduler = Callable[[Awaitable[bool]], None]
"""Schedules a fire-and-forget snapshot POST.

The router never awaits `snapshot_client.append(...)`. The
`MentorAgent` owns the task set; it passes a closure that calls
`asyncio.create_task` and adds the task to `_snapshot_tasks` so the
shutdown drain catches it. See `MentorAgent` Unit 7 wiring.
"""


class EventRouter:
    """Serialized brain dispatcher with coalescing + cost guard."""

    def __init__(
        self,
        *,
        session_id: UUID,
        brain: BrainClient,
        store: RedisSessionStore,
        snapshot_client: SnapshotClient,
        snapshot_scheduler: SnapshotScheduler,
        utterance_queue: UtteranceQueue,
        gate: SpeechCheckGate,
        log_event: LedgerLogger,
        now_ms: Callable[[], int],
    ) -> None:
        self._session_id = session_id
        self._brain = brain
        self._store = store
        self._snapshot_client = snapshot_client
        self._schedule_snapshot = snapshot_scheduler
        self._queue = utterance_queue
        self._gate = gate  # held for future canvas wiring; gate is read by MentorAgent today
        self._log = log_event
        self._now_ms = now_ms

        self._lock = asyncio.Lock()
        self._pending: list[RouterEvent] = []
        self._dispatching: bool = False
        self._in_flight: asyncio.Task[None] | None = None

        # The two router-local counters carried across dispatches.
        self._consecutive_schema_violations = 0
        self._cost_capped = False
        self._escalation_emitted = False

    async def handle(self, event: RouterEvent) -> None:
        """Enqueue an event; spawn `_dispatch` if not already running.

        Returns immediately. The actual brain call runs in
        `_dispatch_loop`. Raises `NotImplementedError` for
        `CANVAS_CHANGE` (M3 path) before touching `pending` or
        `_dispatching`.
        """
        if event.type is EventType.CANVAS_CHANGE:
            log.info("router.canvas_change.deferred_to_m3", t_ms=event.t_ms)
            raise NotImplementedError("canvas_change wires in M3")

        spawn = False
        async with self._lock:
            self._pending.append(event)
            if not self._dispatching:
                self._dispatching = True
                spawn = True
        if spawn:
            self._in_flight = asyncio.create_task(self._dispatch_loop())

    async def cancel_in_flight(self) -> None:
        """Abort the in-flight brain call, if any.

        Safe to call when nothing is running. Awaits the cancellation
        so the caller can rely on the router being quiescent on return
        (modulo `_in_flight` becoming None — see `_dispatch_loop`).
        """
        task = self._in_flight
        if task is None or task.done():
            return
        log.info("router.cancel_in_flight.begin")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        log.info("router.cancel_in_flight.end")

    async def drain(self) -> None:
        """Shutdown: finish in-flight dispatch, drop pending events.

        The session is about to end — writing one more decision into a
        session that's about to 409 on ingest is noise, so we DROP
        pending rather than draining it.
        """
        async with self._lock:
            dropped = len(self._pending)
            self._pending.clear()
        if dropped:
            log.info("router.drain.dropped_pending", count=dropped)
        task = self._in_flight
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def wait_for_idle(self) -> None:
        """Wait until the current dispatch drains `pending` to empty.

        Unlike `drain()`, this does NOT clear pending — it lets the
        in-flight loop process whatever's queued and exit naturally.
        `MentorAgent.handle_user_input` calls this after
        `router.handle(turn_end)` so it can pop a decision from the
        utterance queue once the brain call has finished.
        """
        while True:
            task = self._in_flight
            async with self._lock:
                idle = task is None and not self._dispatching and not self._pending
            if idle:
                return
            if task is not None and not task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                continue
            # Task is done or absent but dispatching flag not yet cleared —
            # yield once so the `_dispatch_loop`'s finally-like flip runs.
            await asyncio.sleep(0)

    async def _dispatch_loop(self) -> None:
        """Drain `pending` one batch at a time until empty.

        The `_dispatching` flag's True→False flip MUST be atomic with
        the "pending is empty" check (invariant I1). If we released the
        flag outside the lock, a `handle()` call arriving in the gap
        between "I observed empty" and "I marked dispatching=False"
        would see `dispatching=True`, append to pending, and return —
        leaving the new event sitting forever with no live loop.

        On `CancelledError`, the in-flight batch is re-prepended to
        `pending` (invariant I2) and `_dispatching` is released atomically
        so a follow-up `handle(...)` spawns a fresh loop.
        """
        while True:
            async with self._lock:
                batch = list(self._pending)
                self._pending.clear()
                if not batch:
                    self._dispatching = False
                    self._in_flight = None
                    return
            try:
                await self._dispatch(batch)
            except asyncio.CancelledError:
                async with self._lock:
                    # Re-prepend in original order (preserves I2) and
                    # release the dispatching flag so the next handle()
                    # spawns a fresh loop.
                    self._pending[:0] = batch
                    self._dispatching = False
                    self._in_flight = None
                log.info(
                    "router.dispatch.cancelled.preserved_pending",
                    preserved=len(batch),
                )
                raise
            except Exception:
                # `_dispatch` already catches Anthropic errors and
                # degrades to stay_silent. Anything escaping here is a
                # router bug — log loudly, drop the batch (the events
                # have already been observed), and keep the loop alive
                # so future events can still dispatch.
                log.exception("router.dispatch.unexpected", batch_size=len(batch))

    async def _dispatch(self, batch: list[RouterEvent]) -> None:
        """One coalesced brain call + state update + snapshot."""
        merged = coalesce(batch)
        # Invariant I3: `t_ms` decided BEFORE any await. The router
        # uses the now-clock (not the merged event's t_ms) so two
        # back-to-back dispatches always have monotonically increasing
        # snapshot timestamps even if the underlying events' t_ms
        # happen to be equal (e.g. clock granularity).
        snapshot_t_ms = max(self._now_ms(), merged.t_ms)
        event_payload = _event_to_payload(merged)

        state = await self._load_state()
        if state is None:
            log.error(
                "router.state_missing",
                session_id=str(self._session_id),
                t_ms=snapshot_t_ms,
            )
            return

        if self._cost_capped or state.cost_usd_total >= state.cost_cap_usd:
            self._cost_capped = True
            decision = BrainDecision.cost_capped()
            log.info(
                "router.cost_capped",
                t_ms=snapshot_t_ms,
                cost_usd_total=state.cost_usd_total,
                cost_cap_usd=state.cost_cap_usd,
            )
        else:
            try:
                decision = await self._brain.decide(
                    state=state,
                    event=event_payload,
                    t_ms=snapshot_t_ms,
                )
            except asyncio.CancelledError:
                # Router's outer loop re-prepends the batch; nothing to
                # write here because the call never completed.
                raise
            except Exception:
                log.exception("router.brain.unexpected", t_ms=snapshot_t_ms)
                decision = BrainDecision.stay_silent("brain_unexpected")

        # Apply state updates BEFORE pushing an utterance: rolling the
        # transcript / decisions log forward is the durable bit; if the
        # process dies before TTS plays, the decision is still in Redis
        # and the snapshot.
        applied_state = await self._apply_decision(state, decision, snapshot_t_ms)

        self._post_snapshot(snapshot_t_ms, applied_state, event_payload, decision)
        self._emit_brain_decision_event(snapshot_t_ms, decision)
        self._update_violation_counter(decision)
        self._maybe_push_utterance(decision, snapshot_t_ms)

    async def _load_state(self) -> SessionState | None:
        return await self._store.load(self._session_id)

    async def _apply_decision(
        self,
        baseline: SessionState,
        decision: BrainDecision,
        t_ms: int,
    ) -> SessionState:
        """Roll the decision's deltas into Redis via CAS.

        On `RedisCasExhaustedError`, log + return `baseline` so the
        snapshot still records the pre-apply state and the utterance
        still goes to the queue. Losing one state update is bad; going
        mute for the rest of the session is worse.
        """
        usage = decision.usage

        def _mutator(current: SessionState | None) -> SessionState:
            base = current or baseline
            updated = base.model_copy(
                update={
                    "tokens_input_total": base.tokens_input_total + usage.tokens_input_total,
                    "tokens_output_total": base.tokens_output_total + usage.output_tokens,
                    "cost_usd_total": base.cost_usd_total + usage.cost_usd,
                }
            )
            # `state_updates` sub-keys (phase_advance, rubric_coverage_delta,
            # new_decision, new_active_argument, session_summary_append) do
            # NOT match SessionState field names. `with_state_updates`
            # translates them and re-validates — a direct `model_copy`
            # would silently drop them.
            if decision.state_updates:
                updated = updated.with_state_updates(dict(decision.state_updates))
            return updated

        try:
            return await self._store.apply(self._session_id, _mutator)
        except RedisCasExhaustedError:
            log.error(
                "router.state.cas_exhausted",
                session_id=str(self._session_id),
                t_ms=t_ms,
            )
            return baseline

    def _post_snapshot(
        self,
        t_ms: int,
        state: SessionState,
        event_payload: dict[str, Any],
        decision: BrainDecision,
    ) -> None:
        usage = decision.usage
        snapshot_row = build_snapshot(
            session_id=self._session_id,
            t_ms=t_ms,
            state=state,
            event_payload=event_payload,
            brain_output=_decision_payload(decision),
            reasoning=decision.reasoning,
            tokens_input=usage.tokens_input_total,
            tokens_output=usage.output_tokens,
        )
        self._schedule_snapshot(
            self._snapshot_client.append(
                session_id=self._session_id,
                t_ms=snapshot_row["t_ms"],
                session_state_json=snapshot_row["session_state_json"],
                event_payload_json=snapshot_row["event_payload_json"],
                brain_output_json=snapshot_row["brain_output_json"],
                reasoning_text=snapshot_row["reasoning_text"],
                tokens_input=snapshot_row["tokens_input"],
                tokens_output=snapshot_row["tokens_output"],
            )
        )

    def _emit_brain_decision_event(self, t_ms: int, decision: BrainDecision) -> None:
        self._log(
            "brain_decision",
            {
                "t_ms": t_ms,
                "decision": decision.decision,
                "priority": decision.priority,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "utterance": decision.utterance,
            },
        )

    def _update_violation_counter(self, decision: BrainDecision) -> None:
        if decision.reason == "schema_violation":
            self._consecutive_schema_violations += 1
            if (
                self._consecutive_schema_violations >= _SCHEMA_VIOLATION_ESCALATION
                and not self._escalation_emitted
            ):
                log.error(
                    "brain.schema_violation.escalated",
                    consecutive=self._consecutive_schema_violations,
                )
                self._log(
                    "brain_decision",
                    {
                        "reason": "schema_violation_escalated",
                        "consecutive": self._consecutive_schema_violations,
                    },
                )
                self._escalation_emitted = True
            return

        # Any non-violated decision (including cost_capped) resets the
        # counter — cost_capped is a router-side abstention, not a
        # brain-output bug.
        if self._consecutive_schema_violations > 0:
            log.info(
                "brain.schema_violation.reset",
                from_count=self._consecutive_schema_violations,
            )
        self._consecutive_schema_violations = 0
        self._escalation_emitted = False

    def _maybe_push_utterance(self, decision: BrainDecision, t_ms: int) -> None:
        if decision.decision != "speak":
            return
        if decision.utterance is None:
            return
        if decision.confidence < _MIN_CONFIDENCE:
            log.info(
                "brain.abstained_low_confidence",
                t_ms=t_ms,
                confidence=decision.confidence,
            )
            return
        if decision.reason == "schema_violation":
            return  # belt-and-braces; from_tool_block won't pair speak+violation
        self._queue.push(
            PendingUtterance(
                text=decision.utterance,
                generated_at_ms=t_ms,
            )
        )


def _event_to_payload(event: RouterEvent) -> dict[str, Any]:
    """Serialize a `RouterEvent` for the brain prompt + snapshot row."""
    return {
        "type": event.type.value,
        "t_ms": event.t_ms,
        **event.payload,
    }


def _decision_payload(decision: BrainDecision) -> dict[str, Any]:
    """Snapshot-row shape for the brain output. Stays JSON-friendly."""
    usage = decision.usage
    return {
        "decision": decision.decision,
        "priority": decision.priority,
        "confidence": decision.confidence,
        "utterance": decision.utterance,
        "reasoning": decision.reasoning,
        "reason": decision.reason,
        "can_be_skipped_if_stale": decision.can_be_skipped_if_stale,
        "state_updates": decision.state_updates,
        "raw_input": decision.raw_input,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cost_usd": usage.cost_usd,
        },
    }


__all__ = ["EventRouter", "LedgerLogger", "SnapshotScheduler"]
