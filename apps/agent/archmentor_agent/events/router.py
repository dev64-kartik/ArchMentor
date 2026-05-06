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
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

import structlog

from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision, DecisionKind
from archmentor_agent.events.coalescer import coalesce
from archmentor_agent.events.types import EventType, RouterEvent
from archmentor_agent.queue import UtteranceQueue
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

# Cost-throttle (refinements R1, R2). Once `_consecutive_stay_silent`
# reaches `_BACKOFF_TRIGGER_N`, the next non-TURN_END / non-PHASE_TIMER
# event is skipped for `min(_BACKOFF_MAX_MS, _BACKOFF_BASE_MS * 2 ** (N-1))`
# ms. The cap protects against pathologically long silences from
# producing 30-min cooldowns.
_BACKOFF_TRIGGER_N = 2
_BACKOFF_BASE_MS = 4_000
_BACKOFF_MAX_MS = 60_000

# Decision reasons that should NOT count toward the consecutive
# stay_silent counter. Real Anthropic stay_silent decisions count;
# router-side skipped paths and cost-cap don't (otherwise the throttle
# eats its own tail — backoff triggers more skips which extend backoff).
_THROTTLE_NONCOUNTING_REASONS = frozenset(
    {"skipped_idempotent", "stay_silent_backoff", "cost_capped"}
)


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


class StreamingTtsHandle(Protocol):
    """Per-dispatch streaming TTS context owned by the router.

    The agent supplies a factory (`streaming_tts_factory`) that builds
    one handle per `_dispatch` invocation. The router passes the
    handle's `listener` to `BrainClient.decide(utterance_listener=...)`,
    which awaits it on each new portion of the streamed `utterance`
    field. After `decide` returns (or raises), the router awaits
    `aclose()` to flush any tail through TTS and tear down the stream.

    `audio_played` reflects whether `listener` was ever invoked with
    non-empty text — used to skip queue.push (streaming already
    delivered the audio) and to suppress R27 (M4 R3b) when partial
    audio reached the candidate's ear.
    """

    @property
    def listener(self) -> Callable[[str], Awaitable[None]]: ...

    @property
    def audio_played(self) -> bool: ...

    async def aclose(self) -> None: ...


StreamingTtsFactory = Callable[[], StreamingTtsHandle]
"""Returns a fresh `StreamingTtsHandle` for one dispatch.

When None (legacy / replay / kill-switch), the router falls through
to the M2/M3 push-to-queue + agent-side-drain flow. When set, the
router opens a handle per dispatch, threads the listener through
`BrainClient.decide`, and skips `_maybe_push_utterance` because the
audio already played live.
"""


DispatchCompleteCallback = Callable[[SessionState], Awaitable[None]]
"""Hook invoked once per dispatch after state is applied (M4 Unit 9).

The router awaits this with the post-apply `SessionState` so the agent
can publish cost telemetry (`ai_telemetry` topic) at the natural
once-per-decision cadence the M4 plan specifies. Errors are caught
inside the router so a mid-teardown publish never aborts the dispatch
loop.
"""


class TelemetryRecorder(Protocol):
    """Subset of `SessionTelemetry` the router needs.

    The router increments three counters: total brain calls (every
    dispatch, including router-side skips so the throttle's
    effectiveness is computable as ``skipped/calls``), idempotent
    short-circuits, and cooldown short-circuits. Decoupled via Protocol
    so the router doesn't import the agent's telemetry module
    directly — keeps the dependency graph one-directional and lets
    tests pass a list-appending fake.
    """

    def record_brain_call(self) -> None: ...
    def record_skipped_idempotent(self) -> None: ...
    def record_skipped_cooldown(self) -> None: ...


class SyntheticUtteranceEmitter(Protocol):
    """Subset of `MentorAgent._emit_synthetic` the router needs.

    Decoupled via Protocol so the router can fire R27's recovery
    utterance through the speech-check gate without importing the agent
    module. The router observes `BrainDecision.reason="brain_timeout"`
    first; the agent owns the gate + TTS hand-off.

    `reason` is a discriminator written to the `ai_utterance` ledger row
    (`synthetic: true`, `reason: "brain_timeout"`) so M5 reports + the
    M6 eval harness can filter synthetic speech out of candidate-speech
    metrics. Fire-and-forget; never awaited from the router.
    """

    def __call__(self, *, text: str, reason: str) -> None: ...


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
        log_event: LedgerLogger,
        now_ms: Callable[[], int],
        synthetic_emitter: SyntheticUtteranceEmitter | None = None,
        recovery_text: str = "",
        pre_dispatch_callback: Callable[[], Awaitable[None]] | None = None,
        streaming_tts_factory: StreamingTtsFactory | None = None,
        telemetry: TelemetryRecorder | None = None,
        dispatch_complete_callback: DispatchCompleteCallback | None = None,
    ) -> None:
        self._session_id = session_id
        self._brain = brain
        self._store = store
        self._snapshot_client = snapshot_client
        self._schedule_snapshot = snapshot_scheduler
        self._queue = utterance_queue
        self._log = log_event
        self._now_ms = now_ms
        self._emit_synthetic = synthetic_emitter
        self._recovery_text = recovery_text
        # Pre-dispatch hook (Unit 2 / R22). The router invokes this
        # before each dispatch loop iteration when a queued speak is
        # fresh (`UtteranceQueue.peek_fresh` is non-None). The agent's
        # `_drain_utterance_queue` is the registered closure; it plays
        # the queued utterance through TTS before the next brain call
        # starts, so a TURN_END speak that lost its dispatch slot to a
        # following CANVAS_CHANGE is delivered, not aged out.
        self._pre_dispatch_callback = pre_dispatch_callback

        # Streaming TTS factory (M4 Unit 4). When set, each dispatch
        # opens a fresh handle; the brain's `utterance` deltas pipe
        # through it in real time (sentence-chunked Kokoro). When None,
        # the legacy push-to-queue + agent-drain path runs unchanged
        # (replay determinism / kill-switch).
        self._streaming_tts_factory = streaming_tts_factory

        # Telemetry recorder (M4 Unit 5 / R4). Optional so the kill-
        # switch and test paths that don't care about session-end counts
        # can omit it. Three increment hooks: every dispatch entry
        # (`record_brain_call`), the idempotent short-circuit, and the
        # cooldown short-circuit. Aggregate ratios are computed at
        # session-end by `MentorAgent.shutdown`.
        self._telemetry = telemetry

        # Post-dispatch callback (M4 Unit 9 / R24). Receives the
        # post-apply `SessionState` so the agent can publish a
        # cost-telemetry frame on `ai_telemetry`. Optional — replay /
        # kill-switch leaves it None and no telemetry leaves the agent.
        # Errors raised by the callback are caught inside the dispatch
        # loop so a mid-teardown publish never aborts a brain decision.
        self._dispatch_complete_callback = dispatch_complete_callback

        self._lock = asyncio.Lock()
        self._pending: list[RouterEvent] = []
        self._dispatching: bool = False
        self._in_flight: asyncio.Task[None] | None = None

        # The two router-local counters carried across dispatches.
        self._consecutive_schema_violations = 0
        self._cost_capped = False
        self._escalation_emitted = False
        # R27: synthetic recovery utterance fires at most once per session.
        # Flips True on attempt regardless of whether the speech-check
        # gate let it through — repeated brain timeouts must not spam
        # the candidate with the same line.
        self._apology_used = False

        # Cost-throttle state (Unit 1 / R20, R21).
        #
        # `_last_input_fingerprint` is a SHA-256 over a curated subset
        # of (state, event_payload) — see `_compute_fingerprint`. When
        # the next dispatch's fingerprint matches AND the prior decision
        # was `stay_silent`, the router short-circuits to
        # `BrainDecision.skipped_idempotent()` (no Anthropic call).
        #
        # `_consecutive_stay_silent` counts back-to-back real
        # stay_silent outcomes (router-side skips do NOT count). At
        # N >= 2, the router enters an exponential-backoff cooldown
        # that skips non-TURN_END / non-PHASE_TIMER events to
        # `BrainDecision.skipped_cooldown(...)`. TURN_END resets the
        # counter; PHASE_TIMER bypasses the cooldown gate but does not
        # reset the counter (refinements R2 — over-budget phase
        # silences shouldn't mask a stuck-silence state).
        self._last_input_fingerprint: str | None = None
        self._last_input_decision_kind: DecisionKind | None = None
        self._consecutive_stay_silent: int = 0
        self._cooldown_until_ms: int = 0

    async def handle(self, event: RouterEvent) -> None:
        """Enqueue an event; spawn `_dispatch` if not already running.

        Returns immediately. The actual brain call runs in
        `_dispatch_loop`. M3 lands `canvas_change`: it flows through the
        same path as `turn_end`, with the coalescer's priority logic
        deciding which event wins when a batch mixes types.
        """
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
            # Pre-dispatch hook (Unit 2 / R22). Drain a fresh queued
            # speak from the prior dispatch BEFORE starting the next
            # brain call so the TURN_END→CANVAS_CHANGE (and inverse)
            # M3-dogfood TTL-drop reproducer doesn't recur.
            await self._maybe_drain_pre_dispatch()
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

    async def _maybe_drain_pre_dispatch(self) -> None:
        """Drain one queued utterance before the next dispatch when fresh.

        Cheap when no callback is registered (kill-switch / test path)
        or no fresh utterance is waiting — the ``peek_fresh`` check is
        ``O(stale-prefix)`` and runs without a brain or network call.
        Errors in the callback are logged but never propagate; the
        dispatch loop must stay alive.
        """
        if self._pre_dispatch_callback is None:
            return
        if self._queue.peek_fresh() is None:
            return
        try:
            await self._pre_dispatch_callback()
            log.info("router.pre_dispatch.drained")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("router.pre_dispatch.callback_error")

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
        # Track wall-clock duration of the dispatch; used in `finally`
        # to extend queued-utterance TTLs by the time we held the slot
        # so a queued speak doesn't age out purely because a competing
        # event delayed its drain (Unit 2 / R23).
        dispatch_start_ms = self._now_ms()

        try:
            state = await self._load_state()
            if state is None:
                log.error(
                    "router.state_missing",
                    session_id=str(self._session_id),
                    t_ms=snapshot_t_ms,
                )
                return

            # Cost-throttle gates run after state load + before the brain
            # call. Order matters: cost-cap is the M2-established final
            # word (router goes silent for the rest of the session); the
            # idempotent/cooldown gates only meaningfully fire on a healthy
            # session.
            fingerprint = self._compute_fingerprint(state, merged)
            # Declared at outer scope so `streamed_audio` below can
            # observe it from any branch (skipped/cost-capped paths
            # never open a handle).
            tts_handle: StreamingTtsHandle | None = None

            if self._cost_capped or state.cost_usd_total >= state.cost_cap_usd:
                self._cost_capped = True
                decision = BrainDecision.cost_capped()
                log.info(
                    "router.cost_capped",
                    t_ms=snapshot_t_ms,
                    cost_usd_total=state.cost_usd_total,
                    cost_cap_usd=state.cost_cap_usd,
                )
            elif self._should_skip_idempotent(fingerprint, merged):
                decision = BrainDecision.skipped_idempotent()
                if self._telemetry is not None:
                    self._telemetry.record_skipped_idempotent()
                log.info(
                    "router.cost_throttle.skipped",
                    reason="skipped_idempotent",
                    t_ms=snapshot_t_ms,
                    consecutive_n=self._consecutive_stay_silent,
                    cooldown_ms=0,
                    event_type=merged.type.value,
                )
            elif self._should_skip_cooldown(snapshot_t_ms, merged):
                cooldown_ms = self._active_cooldown_ms()
                decision = BrainDecision.skipped_cooldown(cooldown_ms=cooldown_ms)
                if self._telemetry is not None:
                    self._telemetry.record_skipped_cooldown()
                log.info(
                    "router.cost_throttle.skipped",
                    reason="stay_silent_backoff",
                    t_ms=snapshot_t_ms,
                    consecutive_n=self._consecutive_stay_silent,
                    cooldown_ms=cooldown_ms,
                    event_type=merged.type.value,
                )
            else:
                # Open a streaming TTS handle for this dispatch when
                # the agent registered a factory. The handle's listener
                # is awaited inline by `BrainClient._decide_streaming`
                # on each new portion of `utterance`; audio reaches the
                # candidate live during the brain call. If no factory
                # is registered, `BrainClient.decide` runs the legacy
                # blocking path (`messages.create`).
                if self._streaming_tts_factory is not None:
                    tts_handle = self._streaming_tts_factory()
                listener = tts_handle.listener if tts_handle is not None else None
                try:
                    decision = await self._brain.decide(
                        state=state,
                        event=event_payload,
                        t_ms=snapshot_t_ms,
                        utterance_listener=listener,
                    )
                except asyncio.CancelledError:
                    # Router's outer loop re-prepends the batch; nothing to
                    # write here because the call never completed. The
                    # streaming TTS handle is closed in the `finally`
                    # below (cancel flushes any pending sentence + tears
                    # down the framework SynthesizeStream).
                    raise
                except Exception:
                    log.exception("router.brain.unexpected", t_ms=snapshot_t_ms)
                    decision = BrainDecision.stay_silent("brain_unexpected")
                finally:
                    if tts_handle is not None:
                        try:
                            await tts_handle.aclose()
                        except Exception:
                            log.exception(
                                "router.streaming_tts.close_error",
                                t_ms=snapshot_t_ms,
                            )

            # Telemetry: count every dispatch entry here, AFTER the
            # decision is fixed — captures cost-capped, idempotent, and
            # cooldown short-circuits on the same axis as a real Anthropic
            # call so the dogfood gate can read throttle effectiveness as
            # ``skipped/calls`` (R4 / Unit 5).
            if self._telemetry is not None:
                self._telemetry.record_brain_call()

            # Apply state updates BEFORE pushing an utterance: rolling the
            # transcript / decisions log forward is the durable bit; if the
            # process dies before TTS plays, the decision is still in Redis
            # and the snapshot.
            applied_state = await self._apply_decision(state, decision, snapshot_t_ms)

            self._post_snapshot(snapshot_t_ms, applied_state, event_payload, decision)
            self._emit_brain_decision_event(snapshot_t_ms, decision)
            self._update_violation_counter(decision)
            self._update_throttle_state(decision, fingerprint, merged, snapshot_t_ms)
            # Skip queue.push when streaming consumed the audio live —
            # the candidate already heard it. Pre-streaming behaviour
            # (push to queue, agent drains via `_drain_utterance_queue`)
            # is preserved when no streaming factory is wired.
            streamed_audio = tts_handle is not None and tts_handle.audio_played
            if not streamed_audio:
                self._maybe_push_utterance(decision, snapshot_t_ms)
            self._maybe_emit_recovery_utterance(decision, partial_audio_played=streamed_audio)

            # Post-dispatch hook (M4 Unit 9 / R24). One frame per
            # dispatch — including router-side skips and cost-cap
            # short-circuits, so the frontend's progress bar reflects
            # the actual call count the dogfood gate sees in
            # `SessionTelemetry.brain_calls_made`. Errors are isolated;
            # a mid-teardown publish must not abort the dispatch loop.
            if self._dispatch_complete_callback is not None:
                try:
                    await self._dispatch_complete_callback(applied_state)
                except Exception:
                    log.exception(
                        "router.dispatch_complete_callback.error",
                        t_ms=snapshot_t_ms,
                    )
        finally:
            # Bump TTLs of every queued utterance by the dispatch's
            # wall-clock duration. Runs even on cancellation so a
            # queued speak from a prior dispatch survives the wait.
            elapsed_ms = self._now_ms() - dispatch_start_ms
            if elapsed_ms > 0:
                self._queue.bump_ttls(elapsed_ms)

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
        # M4 Unit 8 — counter-argument FSM. The schema for
        # `new_active_argument` is `{type: ["object", "null"]}`; the
        # brain emits an object to set/replace, an explicit `null` to
        # close, and OMITS the key for "no change." `BrainDecision.state_updates`
        # is `dict(tool_input.get("state_updates") or {})`, which
        # preserves the inner-key presence and any explicit-null value
        # — so we can derive `key_present` from the decision directly
        # without needing the raw `tool_input` dict the router never
        # receives. Replay-deterministic: pre-M4 snapshots have no
        # `new_active_argument` key → `key_present=False` → resolver
        # preserves prior unchanged (matches M2/M3-era semantics).
        key_present_for_active_argument = "new_active_argument" in decision.state_updates

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
            #
            # Also pass `now_ms=t_ms` so the M4 Unit 8 stale-opener
            # auto-clear (3 min, rounds=0) can fire even when the brain
            # didn't emit `new_active_argument` this turn. Always call
            # the helper (even when `decision.state_updates` is empty)
            # so the auto-clear branch isn't conditional on the brain
            # speaking — that would defeat the safety-net invariant.
            return updated.with_state_updates(
                dict(decision.state_updates),
                key_present_for_active_argument=key_present_for_active_argument,
                now_ms=t_ms,
            )

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
        # `schema_violation_partial_recovery` is the streaming-path
        # discriminator when the post-stream tool_use.input failed
        # validation but partial audio already played (M4 R3d). For
        # the consecutive-violations counter, treat it as a regular
        # schema_violation — the suffix is a payload-level discriminator
        # for replay tooling, not a separate counter category.
        if decision.reason in ("schema_violation", "schema_violation_partial_recovery"):
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

    def _maybe_emit_recovery_utterance(
        self,
        decision: BrainDecision,
        *,
        partial_audio_played: bool = False,
    ) -> None:
        """Fire R27's synthetic recovery utterance on brain timeout.

        Routes through `MentorAgent._emit_synthetic` (the speech-check
        gate is the agent's responsibility — the router doesn't import
        it). The `_apology_used` flag flips on attempt regardless of
        whether the gate let the line through, so repeated brain
        timeouts don't spam the candidate. R24's elapsed-time copy
        carries the visible signal when the gate blocks.

        Two reasons trigger R27 (see `brain/client.py` comment for the
        SDK behaviour that causes the second one):
        - `"brain_timeout"`: `asyncio.wait_for` fired and the SDK
          converted it cleanly to a TimeoutError.
        - `"anthropic_api_connection_during_wait_for"`: the SDK converted
          a wait_for-triggered CancelledError into `APIConnectionError`
          mid-backoff, which the client catches after the wall-clock
          deadline has elapsed. Functionally identical to a timeout from
          the candidate's perspective.

        M4 R3b — when ``partial_audio_played`` is True (streaming TTS
        already pushed at least one delta to the candidate's ear during
        this dispatch), suppress the spoken recovery line. The half-
        sentence the candidate already heard plus R24's elapsed-time
        copy carries the visible signal; layering R27 on top produces
        audible double-talk. Still flip ``_apology_used`` so R27 doesn't
        re-attempt later in the same session.

        No-op when no `synthetic_emitter` was wired (tests / kill-
        switch paths) — the router stays callable without one.
        """
        if self._emit_synthetic is None:
            return
        _recovery_reasons = frozenset({"brain_timeout", "anthropic_api_connection_during_wait_for"})
        if decision.reason not in _recovery_reasons:
            return
        if self._apology_used:
            return
        self._apology_used = True
        if partial_audio_played:
            log.info(
                "agent.r27.suppressed_partial_played",
                reason=decision.reason,
            )
            return
        self._emit_synthetic(text=self._recovery_text, reason=decision.reason or "brain_timeout")

    def _compute_fingerprint(self, state: SessionState, merged: RouterEvent) -> str:
        """Hash a curated subset of state + event payload for the throttle.

        Refinements R1: only the brain-decision-relevant signals enter
        the hash. ``transcript_window_hash``, ``summary_chars``, and
        ``canvas_description_hash`` are deliberately EXCLUDED — summary
        mutations alone (Haiku compaction's parallel CAS appends) and
        canvas description churn alone do NOT change the brain's
        decision surface, which is the throttle's purpose. Compaction's
        transcript-window decrement IS captured via
        ``transcript_turn_count`` (correct: brain reads compressed
        summary plus a smaller window).

        Stable JSON kwargs locked in (``sort_keys``, ``ensure_ascii``,
        ``separators``) to prevent dict-ordering or whitespace drift
        from flickering the hash across runs.
        """
        active_topic = state.active_argument.topic if state.active_argument is not None else None
        payload: dict[str, Any] = {
            "transcript_turn_count": len(state.transcript_window),
            "decisions_count": len(state.decisions),
            "phase": state.phase.value,
            "active_argument_topic": active_topic,
            "fingerprint_payload": _fingerprint_payload(merged),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _should_skip_idempotent(self, fingerprint: str, merged: RouterEvent) -> bool:
        """Return True iff this dispatch should short-circuit to skipped_idempotent.

        Skip rules: fingerprint matches the last call, prior decision
        was stay_silent, and the event type is NOT TURN_END (candidate
        finished a turn — we always take the call). PHASE_TIMER IS
        subject to the fingerprint gate per refinements R2 — bucketed
        ``over_budget_pct_tier`` (Unit 7) keeps the payload stable
        within a tier, so back-to-back PHASE_TIMER ticks at the same
        tier short-circuit and don't burn Anthropic cost.
        """
        if fingerprint != self._last_input_fingerprint:
            return False
        if self._last_input_decision_kind != "stay_silent":
            return False
        return merged.type is not EventType.TURN_END

    def _should_skip_cooldown(self, t_ms: int, merged: RouterEvent) -> bool:
        """Return True iff this dispatch should short-circuit to skipped_cooldown.

        Cooldown rules: ``t_ms < self._cooldown_until_ms`` AND the
        event type is neither TURN_END (candidate finished talking)
        nor PHASE_TIMER (refinements R2 — PHASE_TIMER bypasses cooldown
        because it exists *to break a stuck silence*; without bypass a
        32 s cooldown would eat the very PHASE_TIMER fired at second 30).
        """
        if t_ms >= self._cooldown_until_ms:
            return False
        if merged.type is EventType.TURN_END:
            return False
        return merged.type is not EventType.PHASE_TIMER

    def _active_cooldown_ms(self) -> int:
        """Return the cooldown duration that the current backoff sets."""
        if self._consecutive_stay_silent < _BACKOFF_TRIGGER_N:
            return 0
        return min(
            _BACKOFF_MAX_MS,
            _BACKOFF_BASE_MS * (2 ** (self._consecutive_stay_silent - 1)),
        )

    def _update_throttle_state(
        self,
        decision: BrainDecision,
        fingerprint: str,
        merged: RouterEvent,
        t_ms: int,
    ) -> None:
        """Roll the fingerprint cache + consecutive_stay_silent counter forward.

        Counter rules:
        - TURN_END resets unconditionally (candidate finished a turn).
        - Real ``speak`` decisions reset.
        - Real ``stay_silent`` increments (and arms the cooldown at N >= 2).
        - Skipped paths (idempotent / cooldown) and ``cost_capped`` do
          NOT count — otherwise the throttle eats its own tail.
        - PHASE_TIMER does not reset (refinements R2: an over-budget
          phase silence shouldn't mask a stuck-silence state).

        Fingerprint cache always updates so the next call has a target
        to compare against.
        """
        self._last_input_fingerprint = fingerprint
        self._last_input_decision_kind = decision.decision

        if merged.type is EventType.TURN_END:
            self._reset_throttle("turn_end")
            return

        if decision.decision == "speak":
            self._reset_throttle("speak")
            return

        if decision.reason in _THROTTLE_NONCOUNTING_REASONS:
            return

        if decision.decision == "stay_silent":
            self._consecutive_stay_silent += 1
            if self._consecutive_stay_silent >= _BACKOFF_TRIGGER_N:
                cooldown_ms = self._active_cooldown_ms()
                self._cooldown_until_ms = t_ms + cooldown_ms
                log.info(
                    "router.cost_throttle.cooldown_set",
                    t_ms=t_ms,
                    consecutive_n=self._consecutive_stay_silent,
                    cooldown_ms=cooldown_ms,
                )

    def _reset_throttle(self, reason: str) -> None:
        """Clear consecutive_stay_silent + cooldown after a real signal."""
        if self._consecutive_stay_silent > 0 or self._cooldown_until_ms > 0:
            log.info(
                "router.cost_throttle.reset",
                reason=reason,
                prior_consecutive_n=self._consecutive_stay_silent,
            )
        self._consecutive_stay_silent = 0
        self._cooldown_until_ms = 0

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


def _fingerprint_payload(event: RouterEvent) -> dict[str, Any]:
    """Curated event-payload subset for the cost-throttle fingerprint.

    Distinct from ``_event_to_payload`` (which feeds the brain prompt
    and snapshot row, where ``t_ms`` IS material). The fingerprint
    needs an order-stable, time-stripped projection so two
    semantically-identical events fingerprint-match across the
    wall-clock drift of two dispatches:

    - ``t_ms`` is dropped — it flickers every dispatch.
    - ``merged_from`` is dropped — coalescer drain order is not byte-stable.
    - ``concurrent_transcripts`` (HIGH-priority batches) and
      ``transcripts`` (TURN_END collapse batches) are sorted before
      hashing so two arrival orders produce the same hash.
    """
    payload: dict[str, Any] = {"type": event.type.value}
    for key, value in event.payload.items():
        if key in ("t_ms", "merged_from"):
            continue
        if key in ("concurrent_transcripts", "transcripts") and isinstance(value, list):
            payload[key] = sorted(str(item) for item in value)
            continue
        payload[key] = value
    return payload


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


__all__ = [
    "DispatchCompleteCallback",
    "EventRouter",
    "LedgerLogger",
    "SnapshotScheduler",
    "SyntheticUtteranceEmitter",
    "TelemetryRecorder",
]
