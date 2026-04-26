"""LiveKit agent worker entrypoint.

Responsibilities in M2:
- Join the LiveKit room dispatched to this worker.
- Speak a static opening line via `session.say()`.
- Detect candidate turn-ends via the framework's built-in VAD + STT.
- Initialize `SessionState` in Redis and dispatch every `turn_end` to
  the event router (`EventRouter.handle`). The router coalesces
  concurrent events, runs Anthropic tool-use via `BrainClient`, writes
  a snapshot to Postgres, and pushes any `speak` utterance to the
  queue. `handle_user_input` waits for the dispatch to finish then
  drains the utterance queue to TTS under the speech-check gate.
- Carry forward M1's append-only ledger writes (candidate utterance,
  AI utterance, brain-decision events) — all still fire-and-forget.
- Keep pre-VAD noise gating in-path so keyboard/trackpad clicks don't
  fire false turn-ends.

`settings.brain_enabled = False` preserves the M1 static-ack path so
STT/TTS iteration isn't held hostage by a broken Anthropic key. The
branch lives in `_run_turn`; the brain components are built only when
enabled to avoid construction overhead + failed validations on the
cold path.

Audio deps (`pywhispercpp`, `streaming-tts`) are optional; this module
imports cleanly in CI where those wheels aren't installed. The import
of the real STT/TTS adapters happens only when `entrypoint` is called.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
)
from livekit.rtc.participant import PublishDataError

from archmentor_agent.api_client.bootstrap import BootstrapFetchError, fetch_session_bootstrap
from archmentor_agent.audio.stt import _WHISPER_PROMPT_ECHO_STEMS
from archmentor_agent.brain.bootstrap import (
    DEV_PROMPT_VERSION,
    build_dev_problem_card,
)
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.canvas import CanvasSnapshotClient, CanvasSnapshotClientConfig, parse_scene
from archmentor_agent.config import Settings, get_settings
from archmentor_agent.events.router import EventRouter
from archmentor_agent.events.types import EventType, RouterEvent, default_priority
from archmentor_agent.ledger import LedgerClient, LedgerConfig
from archmentor_agent.queue import SpeechCheckGate, UtteranceQueue
from archmentor_agent.snapshots.client import SnapshotClient, SnapshotClientConfig
from archmentor_agent.state.redis_store import RedisCasExhaustedError, RedisSessionStore
from archmentor_agent.state.session_state import (
    CanvasState,
    PendingUtterance,
    ProblemCard,
    SessionState,
    TranscriptTurn,
)

AiState = Literal["speaking", "listening", "thinking"]
AI_STATE_TOPIC = "ai_state"


class CanvasNoBaselineStateError(Exception):
    """Raised when no SessionState baseline exists in Redis for a canvas CAS apply.

    Distinct from `RedisCasExhaustedError` (retry budget exhausted) so log
    lines carry `canvas.no_baseline_state` vs `canvas.cas_exhausted` and
    operators can distinguish eviction/pre-init from contention.
    """


# Default 45-minute session budget, mirrors `InterviewSession.duration_s_planned`.
_DEFAULT_SESSION_SECONDS = 2700

# Rolling-transcript window cap (turns). The brain reads the full
# `transcript_window` on every call, so an unbounded list would inflate
# every prompt's token count linearly with session length. 30 turns =
# roughly 2-3 minutes of conversation, matching the M2 plan's "verbatim,
# last 2-3 min" intent. M4's Haiku session-summary compression replaces
# this hard cap with summarise-and-trim semantics; until then this is
# the single back-pressure on prompt size.
_TRANSCRIPT_WINDOW_CAP = 30


class TranscriptNoBaselineStateError(Exception):
    """Raised when no SessionState baseline exists in Redis for a transcript CAS apply.

    Mirrors `CanvasNoBaselineStateError` so eviction / pre-init paths
    log under their own key (`agent.transcript.no_baseline_state`)
    rather than blurring with CAS contention exhaustion.
    """


class _UserInputEvent(Protocol):
    """Minimal shape of livekit-agents `user_input_transcribed` events.

    The framework doesn't export a typed event class for this, so we
    declare the subset of fields we rely on here. If the SDK renames
    `is_final` or `transcript`, ty flags every call site rather than
    letting a `getattr(..., False)` fallback silently drop turns.
    """

    is_final: bool
    transcript: str


log = structlog.get_logger(__name__)

OPENING_UTTERANCE = (
    "Hi — I'm your interviewer today. Take a moment to read the problem, "
    "and when you're ready, walk me through your approach. "
    "I'll take a moment to think between turns — feel free to keep talking "
    "if I'm quiet."
)
TURN_ACK_UTTERANCE = "Got it. Keep going when you're ready."

# Recovery utterance the agent attempts at most once per session when the
# brain times out (R27). Persona-consistent: no "sorry", no "I lost my
# train of thought" — both came up as junior-engineer voice in the
# refinements review (Q2). Routes through the speech-check gate; if the
# candidate is mid-speech, dropped silently and R24's elapsed-time copy
# carries the visible signal.
SYNTHETIC_RECOVERY_UTTERANCE = "Let me come back to that — please continue."

# LiveKit text-stream topic the browser publishes Excalidraw scenes on.
# `ai_state` stays on `publishData` because its payload is fixed-size
# (~30 bytes); canvas scenes use text streams because LiveKit chunks them
# transparently above the SCTP per-frame limit (~16 KiB). See plan Key
# Technical Decisions: "fixed-size telemetry → publishData; bounded-but-
# can-grow content → text streams."
CANVAS_SCENE_TOPIC = "canvas-scene"

# Canvas event rate limit per R22 / Q9 — 60 events/min/session, applied
# whether or not the session is cost-capped. Defends the ledger against
# a flooded publisher (a misbehaving browser or scripted client). The
# cap is generous: at the 1-second `onChange` throttle the browser ships,
# a candidate would have to draw continuously for 60 seconds to hit it.
CANVAS_RATE_LIMIT_EVENTS = 60
CANVAS_RATE_LIMIT_WINDOW_S = 60.0


# Whisper emits bracketed sound tags on silence/non-speech:
# `[Music]`, `[BLANK_AUDIO]`, `[Silence]`, etc.
_HALLUCINATION_TAGS = {
    "[music]",
    "[blank_audio]",
    "[silence]",
    "[noise]",
    "(music)",
    "(silence)",
    "(blank_audio)",
}

# Whisper was trained heavily on YouTube; on silence or near-silence,
# small models (base.en, tiny.en) frequently hallucinate stock outro
# phrases. Match against stems so trailing punctuation/whitespace
# variations all get caught. Larger models (small.en+) hallucinate
# these too, just less often — keep the filter as a safety net.
_HALLUCINATION_PHRASES = (
    "thanks for watching",
    "thank you for watching",
    "see you in the next video",
    "see you next time",
    "subscribe to my channel",
    "and the one that i love",
    "we'll see you next time",
    "this video is sponsored by",
    "don't forget to like",
    "please subscribe",
    "bye bye",
)

# Short stock phrases whisper regurgitates on silence / low-SNR audio,
# observed repeatedly in a noisy-environment manual test (2026-04-23).
# These are matched against the WHOLE normalized transcript, not as a
# substring — "thank you" inside "thank you, so the capacity is…" is
# legitimate and must reach the brain; "Thank you." as the entire
# transcript on a quiet 2-second buffer is almost always hallucination.
#
# Only entries here that are almost never a standalone meaningful
# candidate turn in a system design interview. "ok" / "okay" / "yes"
# / "no" / "right" deliberately NOT included — those are legitimate
# short acks and are covered by
# `test_legitimate_technical_utterances_are_not_hallucinations`.
_HALLUCINATION_EXACT_PHRASES = frozenset(
    {
        "thank you",
        "thanks",
        "thank you very much",
        "thank you so much",
        "bye",
        "goodbye",
        "bye bye",
        "you",
    }
)

# Runs of whitespace + the punctuation characters listed below get
# collapsed to a single space during normalization. Covers trailing
# periods/commas, hyphens + Unicode dashes (whisper emits "-" / "—"
# on short ambiguous buffers), and ellipsis. A regex rather than
# `str.strip(...)` so internal patterns like ". . ." also collapse.
# Intentional em-dash (U+2014) and en-dash (U+2013) — whisper emits
# both; the ruff ambiguous-dash warning is expected here.
_PUNCT_WS = re.compile(r"[\s.!?,;:\-—–…]+")  # noqa: RUF001


def _is_whisper_hallucination(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    # Bracketed tags ([Music], [BLANK_AUDIO], …) — check before the
    # punctuation normalization chews the brackets.
    if lowered in _HALLUCINATION_TAGS or (lowered.startswith("[") and lowered.endswith("]")):
        return True
    normalized = _PUNCT_WS.sub(" ", lowered).strip()
    if not normalized:
        return True
    # Whisper regurgitates its `initial_prompt` on short/quiet/ambiguous
    # buffers. Any transcript containing a sentence-stem from the prompt
    # is an echo, not candidate speech — drop it before it reaches the
    # brain (otherwise the mentor burns tokens responding to its own
    # priming text). See `audio/stt._WHISPER_PROMPT_ECHO_STEMS`.
    if any(stem in normalized for stem in _WHISPER_PROMPT_ECHO_STEMS):
        return True
    if normalized in _HALLUCINATION_EXACT_PHRASES:
        return True
    return any(phrase in normalized for phrase in _HALLUCINATION_PHRASES)


@dataclass(frozen=True, slots=True)
class _BrainWiring:
    """Bundle of per-session brain collaborators.

    Grouping them here keeps `MentorAgent.__init__` readable and gives
    tests one seam to override all of them (or swap individual
    components) rather than threading six kwargs through every test.
    """

    brain: BrainClient
    store: RedisSessionStore
    snapshot_client: SnapshotClient
    canvas_snapshot_client: CanvasSnapshotClient
    router: EventRouter
    queue: UtteranceQueue
    gate: SpeechCheckGate


def build_initial_session_state(
    *,
    cost_cap_usd: float,
    prompt_version: str = DEV_PROMPT_VERSION,
    now: datetime | None = None,
    problem: ProblemCard | None = None,
) -> SessionState:
    """Assemble the `SessionState` the brain starts from on `on_enter`.

    Split out so Unit 7's integration tests can build a seed state
    without invoking the full entrypoint.

    The `problem` parameter accepts a `ProblemCard` built by the caller
    (production: fetched from the bootstrap API; replay/dev: built from
    `build_dev_problem_card()`). When omitted, falls back to
    `build_dev_problem_card()` so existing tests and seed-dev flows keep
    working without changes.
    """
    return SessionState(
        problem=problem if problem is not None else build_dev_problem_card(),
        system_prompt_version=prompt_version,
        started_at=now or datetime.now(UTC),
        elapsed_s=0,
        remaining_s=_DEFAULT_SESSION_SECONDS,
        cost_cap_usd=cost_cap_usd,
    )


class MentorAgent(Agent):
    """Voice-loop agent with M2's tool-use brain wiring.

    The M1 static-ack path (`TURN_ACK_UTTERANCE`) is preserved as an
    explicit fallback when `settings.brain_enabled=False` so STT/TTS
    iteration isn't blocked by a broken Anthropic key or quota.
    """

    def __init__(
        self,
        *,
        session_id: UUID,
        ledger: LedgerClient,
        room: rtc.Room,
        brain_enabled: bool,
        brain: _BrainWiring | None,
        bootstrap_problem: ProblemCard | None = None,
    ) -> None:
        super().__init__(
            instructions=(
                "You are ArchMentor's interview coordinator. The brain "
                "pipeline makes all speaking decisions via tool-use."
            )
        )
        self._session_id = session_id
        self._ledger = ledger
        self._room = room
        self._brain_enabled = brain_enabled
        self._brain = brain
        # ProblemCard fetched from the bootstrap API in the production
        # path; None on replay/dev paths (falls back to build_dev_problem_card).
        self._bootstrap_problem = bootstrap_problem
        self._t0_ms: int | None = None
        # Set once the opening utterance has finished playing. The
        # framework refuses follow-up `session.say()` while the agent
        # is mid-speech ("speech scheduling is paused"), and any STT
        # transcript captured during the intro is almost always whisper
        # hallucinating on the agent's own audio bleed-in. The event
        # listener checks this and drops pre-intro user input.
        self.opening_complete = asyncio.Event()
        # Fire-and-forget ledger writes go here. The entrypoint's
        # finally block drains this set before closing the HTTP client
        # so in-flight writes don't get cut off mid-request.
        self._ledger_tasks: set[asyncio.Task[bool]] = set()
        # Same discipline for brain-snapshot POSTs. Kept separate so the
        # drain order (snapshots after router.drain, before client
        # aclose) is explicit in `entrypoint`.
        self._snapshot_tasks: set[asyncio.Task[bool]] = set()
        # Canvas snapshot task set — separate from brain snapshots so
        # the entrypoint can drain them independently and the failure
        # mode for one can't poison the other.
        self._canvas_tasks: set[asyncio.Task[bool]] = set()
        # Sliding-window rate limiter for canvas events (R22 / Q9). A
        # `time.monotonic()` timestamp goes in on every text-stream
        # message; entries older than the window are dropped on each
        # check. 60 events / 60 s by default. The list grows linearly
        # with publish rate but is bounded by the window — under normal
        # operation it stays at most a few dozen entries long.
        self._canvas_event_history: list[float] = []
        # Synthetic recovery utterance tasks (R27). Tracked separately
        # from `_ledger_tasks` because the task return type differs
        # (`None`, not `bool`) and shutdown drains them in their own
        # asyncio.gather to keep the typing tight.
        self._synthetic_tasks: set[asyncio.Task[None]] = set()
        # Serializes `_drain_utterance_queue` across concurrent
        # `_run_brain_turn` tasks. Without this, two rapid turn_end
        # finals (e.g. the noisy-room "Thank you." hallucination
        # cascade) each `wait_for_idle` + drain simultaneously, and
        # the pops race — observed as two back-to-back
        # `queue.delivered` events in the same log second and a
        # half-spoken utterance being cut off by the next `say()`.
        # A new drain that finds the lock held skips its pop entirely
        # (the in-flight say is the user's most recent utterance; its
        # own utterance, if any, gets dropped by TTL or by a later
        # `clear_stale_on_new_turn`).
        self._say_lock = asyncio.Lock()

    def attach_brain(self, wiring: _BrainWiring) -> None:
        """Inject the brain collaborators after construction.

        Separate from `__init__` because `build_brain_wiring` needs a
        constructed agent to bind its `schedule_snapshot` / `log_event`
        methods as callbacks. The pattern is: construct the agent,
        build the wiring with it, attach. Tests that exercise the
        kill-switch path skip this step entirely — `_brain` stays None.
        """
        if not self._brain_enabled:
            raise RuntimeError(
                "attach_brain called but brain_enabled=False — "
                "the static-ack path does not consume brain wiring."
            )
        self._brain = wiring

    async def on_enter(self) -> None:
        log.info("agent.on_enter.begin", session_id=str(self._session_id))
        self._t0_ms = int(time.monotonic() * 1000)
        # `opening_complete` must be set no matter what. If we leave it
        # unset (TTS error, ledger error, cancellation), the STT event
        # handler drops every user turn for the life of the session —
        # the session is alive and connected but effectively deaf.
        # Surface the error, then unblock STT in `finally`.
        try:
            await self._initialize_brain_state(problem=self._bootstrap_problem)
            await self._publish_state("speaking")
            log.info("agent.opening.say.begin", text=OPENING_UTTERANCE)
            # `allow_interruptions=False` keeps the intro playing through
            # LiveKit's built-in VAD barge-in. Without this, a noisy
            # room (keyboard clatter, ambient voices) triggers VAD >3s
            # into the opening — past the AEC warmup window — and cuts
            # the intro mid-sentence. Barge-in is re-enabled by default
            # for every subsequent `session.say(...)` on the turn_end
            # path, where the candidate SHOULD be able to interrupt.
            handle = self.session.say(OPENING_UTTERANCE, allow_interruptions=False)
            await handle.wait_for_playout()
            log.info("agent.opening.say.end")
            opening_t_ms = self._now_relative_ms()
            self._log("utterance_ai", {"text": OPENING_UTTERANCE, "speaker": "ai"})
            await self._append_transcript_turn(
                speaker="ai", text=OPENING_UTTERANCE, t_ms=opening_t_ms
            )
            await self._publish_state("listening")
        except Exception:
            log.exception("agent.on_enter.failed")
            # Best effort: put the UI back into a sensible state even
            # though we couldn't play the intro.
            try:
                await self._publish_state("listening")
            except Exception:
                # Double-fault: the publish already catches transport
                # errors internally; anything escaping here is either a
                # programming error or teardown. We're already in the
                # error handler of on_enter — log and drop so the
                # original exception still propagates to the caller.
                log.exception("agent.on_enter.publish_listening_failed")
            raise
        finally:
            self.opening_complete.set()
            log.info("agent.on_enter.end")

    async def _initialize_brain_state(
        self,
        *,
        problem: ProblemCard | None = None,
    ) -> None:
        """Seed Redis with a fresh SessionState for this session.

        Skipped when the brain is disabled — the M1 fallback path has
        no session-state consumer so a Redis round-trip would be wasted
        (and fail if Redis isn't running, which is a valid M1-only
        dev configuration).

        The `problem` kwarg carries the ProblemCard fetched from the
        bootstrap API in the production path (see `entrypoint`). When
        None (replay / seed-dev paths), falls back to `build_dev_problem_card`.
        """
        if not self._brain_enabled or self._brain is None:
            return
        state = build_initial_session_state(
            # M2 reads cost cap from `Settings.cost_cap_usd` if/when we
            # add it; until then the SessionState default (5.0) is the
            # intended ceiling — matches the API's
            # `InterviewSession.cost_cap_usd` column default.
            cost_cap_usd=5.0,
            problem=problem,
        )
        await self._brain.store.put(self._session_id, state)
        log.info(
            "agent.state.seeded",
            session_id=str(self._session_id),
            problem_slug=state.problem.slug,
            cost_cap_usd=state.cost_cap_usd,
        )

    async def handle_user_input(self, text: str) -> None:
        """Called from the session's `user_input_transcribed` final event.

        Hallucination filter + ledger write + state="thinking" are
        shared; the brain dispatch runs when `brain_enabled`, otherwise
        the M1 static-ack path runs. Never raises: voice-loop errors
        degrade to silence, not session death.
        """
        if _is_whisper_hallucination(text):
            log.info("agent.user_input.dropped_hallucination", text=text)
            return

        turn_t_ms = self._now_relative_ms()
        self._log("utterance_candidate", {"text": text, "speaker": "candidate"})
        # Append BEFORE the brain dispatch so the brain reads a state
        # that includes the just-spoken turn (read-after-CAS contract,
        # mirrors R23 for canvas_state). On the static-ack path this is
        # a no-op because there's no brain wiring.
        await self._append_transcript_turn(speaker="candidate", text=text, t_ms=turn_t_ms)

        if not self._brain_enabled or self._brain is None:
            await self._run_static_ack_turn()
            return

        await self._run_brain_turn(text=text, turn_t_ms=turn_t_ms)

    async def _run_static_ack_turn(self) -> None:
        """M1 fallback — speak the fixed acknowledgement.

        Kept as an explicit branch (not a log-only stub) so the kill
        switch survives a broken Anthropic key. The wiring tests flip
        `brain_enabled` between cases to cover both paths.
        """
        await self._publish_state("thinking")
        log.info("agent.ack.begin", ack=TURN_ACK_UTTERANCE)
        await self._publish_state("speaking")
        try:
            await self._say(TURN_ACK_UTTERANCE)
        except RuntimeError as exc:
            # Tab close / disconnect races the session teardown.
            log.warning("agent.say_skipped", reason=str(exc))
            await self._publish_state("listening")
            return
        log.info("agent.ack.end")
        ack_t_ms = self._now_relative_ms()
        self._log("utterance_ai", {"text": TURN_ACK_UTTERANCE, "speaker": "ai"})
        await self._append_transcript_turn(speaker="ai", text=TURN_ACK_UTTERANCE, t_ms=ack_t_ms)
        await self._publish_state("listening")

    async def _run_brain_turn(self, *, text: str, turn_t_ms: int) -> None:
        """Brain path: dispatch to router, wait for decision, speak if any."""
        if self._brain is None:
            raise RuntimeError("_run_brain_turn called with no brain wiring")
        brain = self._brain

        # The interim handler already flipped the gate to "done
        # speaking" when the final arrived; we mirror that explicitly
        # here in case the final came in without a matching interim
        # (whisper sometimes skips interims on short buffers).
        brain.gate.mark_done_speaking()

        # Drop any utterance still queued from a PREVIOUS brain dispatch
        # that predates this turn. The 10 s TTL is the fallback; a new
        # turn is the primary freshness signal — the candidate has now
        # added context that may have invalidated the older reply.
        brain.queue.clear_stale_on_new_turn(turn_t_ms)

        await self._publish_state("thinking")

        event = RouterEvent(
            type=EventType.TURN_END,
            t_ms=turn_t_ms,
            payload={"text": text},
        )
        try:
            await brain.router.handle(event)
        except NotImplementedError:
            # canvas_change is the only type that raises at M2; turn_end
            # never should. If this fires it's a type-system regression
            # — log and fall through to listening.
            log.exception("agent.router.unexpected_not_implemented")
            await self._publish_state("listening")
            return

        # Let the dispatch loop run the Anthropic call + snapshot post
        # + queue push. `wait_for_idle` does NOT clear pending, so it
        # picks up anything that lands during the wait too.
        try:
            await brain.router.wait_for_idle()
        except asyncio.CancelledError:
            # The router's dispatch may be cancelled by an interim
            # transcript (barge-in). Propagate so the caller task
            # unwinds cleanly; another turn_end will re-dispatch.
            raise

        await self._drain_utterance_queue()

    async def _drain_utterance_queue(self) -> None:
        """Pop the next fresh utterance and speak it.

        Only one utterance per pause — a single call to `session.say`
        per `handle_user_input` invocation. The speech-check gate
        isn't consulted on the turn_end path because a final transcript
        already *is* the "candidate is done" signal; barge-in races
        during the brain call are handled via `cancel_in_flight` from
        the interim handler, not here. The gate becomes load-bearing
        in M3+ when `long_silence` and `canvas_change` events can push
        utterances outside a natural turn-end.

        Concurrency: two rapid `handle_user_input` finals can each
        land here simultaneously (the router is serialized; the
        handler tasks are not). ``_say_lock`` ensures only one
        ``session.say`` runs at a time. If the lock is already held,
        we skip the pop entirely — the in-flight say is the caller's
        most recent output; popping our own would cut it off with
        another ``say`` that in a noisy-room hallucination cascade is
        almost always stale.
        """
        if self._brain is None:
            raise RuntimeError("_drain_utterance_queue called with no brain wiring")
        brain = self._brain

        if self._say_lock.locked():
            log.info("agent.drain.skipped_concurrent")
            await self._publish_state("listening")
            return

        async with self._say_lock:
            utterance = brain.queue.pop_if_fresh()
            if utterance is None:
                await self._publish_state("listening")
                return

            await self._publish_state("speaking")
            try:
                await self._say(utterance.text)
            except RuntimeError as exc:
                log.warning("agent.say_skipped", reason=str(exc))
                await self._publish_state("listening")
                return
            ai_t_ms = self._now_relative_ms()
            self._log("utterance_ai", {"text": utterance.text, "speaker": "ai"})
            await self._append_transcript_turn(speaker="ai", text=utterance.text, t_ms=ai_t_ms)
            await self._publish_state("listening")

    async def _say(self, text: str) -> None:
        """TTS hand-off seam.

        Production calls `self.session.say(text)`. Tests override this
        method to record the utterance without needing an active
        `AgentSession` (the base class's `session` property raises
        "agent is not running" without a live activity context).
        """
        await self.session.say(text)

    async def handle_interim_transcript(self, text: str) -> None:
        """Mark the gate + cancel any in-flight brain call (barge-in).

        Called from the `user_input_transcribed` handler whenever
        `is_final=False`. No-op when the brain is disabled — the
        static-ack path doesn't care whether the candidate is
        mid-sentence.
        """
        if not self._brain_enabled or self._brain is None:
            return
        self._brain.gate.mark_speaking()
        log.info("agent.interim.cancel_in_flight", text_preview=text[:40])
        await self._brain.router.cancel_in_flight()

    async def shutdown(self) -> None:
        """Graceful teardown — finish in-flight work, then clean up.

        Order matters (plan Unit 7 "Shutdown drain ordering"):
        router.drain() finishes the current dispatch and drops pending;
        `_snapshot_tasks` drains post-dispatch snapshot POSTs;
        `_ledger_tasks` drains everything else; `store.delete` removes
        the no-TTL session key; client aclose() frees HTTP pools.
        """
        if self._brain_enabled and self._brain is not None:
            log.info("agent.shutdown.router_drain.begin")
            await self._brain.router.drain()
            log.info("agent.shutdown.router_drain.end")
            if self._snapshot_tasks:
                log.info(
                    "agent.shutdown.drain_snapshot_tasks",
                    count=len(self._snapshot_tasks),
                )
                await asyncio.gather(*self._snapshot_tasks, return_exceptions=True)
            if self._canvas_tasks:
                log.info(
                    "agent.shutdown.drain_canvas_tasks",
                    count=len(self._canvas_tasks),
                )
                await asyncio.gather(*self._canvas_tasks, return_exceptions=True)
            if self._synthetic_tasks:
                log.info(
                    "agent.shutdown.drain_synthetic_tasks",
                    count=len(self._synthetic_tasks),
                )
                await asyncio.gather(*self._synthetic_tasks, return_exceptions=True)

        if self._ledger_tasks:
            log.info(
                "agent.shutdown.drain_ledger_tasks",
                count=len(self._ledger_tasks),
            )
            await asyncio.gather(*self._ledger_tasks, return_exceptions=True)

        if self._brain_enabled and self._brain is not None:
            # Explicit cleanup — Redis session keys have no TTL.
            # A crashed worker leaves an orphan key until M6's stale-
            # session reaper; surface the delete here in logs so its
            # absence is visible during teardown debugging.
            with contextlib.suppress(Exception):
                await self._brain.store.delete(self._session_id)

    async def _publish_state(self, state: AiState) -> None:
        """Tell the browser which phase the agent is in.

        The frontend stays connected via a single audio track that never
        unsubscribes between utterances, so it can't infer state from
        track events. Surfacing it explicitly via a data message lets
        the UI flip the indicator promptly and tells the candidate
        when it's their turn to speak.
        """
        # json.dumps of a fixed-shape dict can't fail at runtime — keep
        # it outside the transport try/except so a future shape change
        # surfaces as an error, not a swallowed warning.
        payload = json.dumps({"ai_state": state})
        try:
            await self._room.local_participant.publish_data(
                payload,
                topic=AI_STATE_TOPIC,
            )
        except (ConnectionError, OSError, RuntimeError, PublishDataError) as exc:
            # Data publish must never break the voice loop — room is
            # mid-teardown (LiveKit FFI raises PublishDataError when the
            # engine is closed during disconnect) or the participant
            # isn't connected yet. The on_enter() catch path retries
            # this call as a "best effort" recovery; without swallowing
            # PublishDataError here, the disconnect-mid-TTS path emits
            # two stack traces per session for what is benign teardown.
            log.warning("agent.publish_state_failed", state=state, reason=str(exc))

    def _log(self, event_type: str, payload: dict[str, object]) -> None:
        """Schedule a fire-and-forget ledger append.

        Awaiting the ledger inline blocks the voice loop whenever the
        API is slow (a 5xx retry storm can stall a turn for seconds).
        Ledger writes are best-effort from the agent's side — the
        `LedgerClient` handles retries and drops on permanent failure
        — so we schedule the task and move on. The entrypoint drains
        the task set before closing the HTTP client.
        """
        # Snapshot `t_ms` synchronously: once we return control to the
        # caller the relative clock may drift before the task runs.
        t_ms = self._now_relative_ms()
        task = asyncio.create_task(
            self._ledger.append(
                session_id=self._session_id,
                t_ms=t_ms,
                event_type=event_type,
                payload=payload,
            )
        )
        self._ledger_tasks.add(task)
        task.add_done_callback(self._ledger_tasks.discard)

    def schedule_snapshot(self, coro: Awaitable[bool]) -> None:
        """Router-side callback — schedule a snapshot POST + track it.

        The router calls this synchronously from its dispatch loop; we
        wrap the coroutine in a task so the dispatch doesn't block on
        the POST, and retain the task so `shutdown()` can drain it.

        The parameter type mirrors the router's ``SnapshotScheduler``
        alias so a type-checker catches a mismatched callback shape.
        """
        task = asyncio.ensure_future(coro)
        self._snapshot_tasks.add(task)
        task.add_done_callback(self._snapshot_tasks.discard)

    def schedule_canvas_snapshot(self, coro: Awaitable[bool]) -> None:
        """Track a fire-and-forget canvas snapshot POST."""
        task = asyncio.ensure_future(coro)
        self._canvas_tasks.add(task)
        task.add_done_callback(self._canvas_tasks.discard)

    def emit_synthetic(self, *, text: str, reason: str) -> None:
        """Router → agent synthetic-utterance hand-off (R27).

        Spawns `_emit_synthetic_async` as a task because the router
        calls this synchronously (its dispatch loop's last step) and we
        must not block it on a TTS playout. The task tracks against
        `_synthetic_tasks` so shutdown drains it.
        """
        task = asyncio.create_task(self._emit_synthetic_async(text=text, reason=reason))
        self._synthetic_tasks.add(task)
        task.add_done_callback(self._synthetic_tasks.discard)

    async def _emit_synthetic_async(self, *, text: str, reason: str) -> None:
        """Run the speech-check gate + say + ledger-write for R27.

        Gate-blocked path is silent: R24's elapsed-time copy already
        carries the visible signal, so layering a barge-in apology on
        top would land mid-sentence.
        """
        if self._brain is None:
            return
        if self._brain.gate.is_candidate_speaking():
            log.info("agent.synthetic.gate_blocked", reason=reason, text=text)
            return
        await self._publish_state("speaking")
        try:
            await self._say(text)
        except RuntimeError as exc:
            log.warning("agent.synthetic.say_skipped", reason=str(exc))
            await self._publish_state("listening")
            return
        # `synthetic: true` + `reason` discriminators let M5 reports +
        # the M6 eval harness filter synthetic speech out of candidate-
        # facing utterance metrics.
        self._log(
            "utterance_ai",
            {"text": text, "speaker": "ai", "synthetic": True, "reason": reason},
        )
        await self._publish_state("listening")

    async def on_canvas_scene_payload(self, raw: str) -> None:
        """Handle a single `canvas-scene` text-stream message from the browser.

        Pipeline (R7-R23 / R17):

        1. Bounded JSON parse — `ValueError` / `RecursionError` becomes a
           `canvas_parse_error` ledger row, no router dispatch.
        2. Server-side `files` strip (R17 defense-in-depth — the browser
           also strips, but a scripted client might forget).
        3. Sliding-window rate-limit (R22 / Q9).
        4. `parse_scene` produces the fenced text the brain reads.
        5. CAS apply `canvas_state.description` BEFORE dispatching the
           router event (R23) so the brain typically sees the canvas it's
           reasoning about.
        6. Dispatch `RouterEvent(CANVAS_CHANGE, HIGH)` so the coalescer
           preempts a concurrent turn_end (R11).
        7. Schedule fire-and-forget canvas-snapshot POST (R10).
        8. Write a `canvas_change` ledger event with `parsed_text` (R21).

        Never raises — canvas-scene errors must not break the voice loop.
        """
        if not self._brain_enabled or self._brain is None:
            log.info("agent.canvas.dropped_brain_disabled")
            return

        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            self._log(
                "canvas_parse_error",
                {"error": exc.__class__.__name__, "detail": str(exc)[:200]},
            )
            log.warning("agent.canvas.parse_error", error=str(exc)[:200])
            return

        if not isinstance(payload, dict):
            self._log(
                "canvas_parse_error",
                {"error": "TypeError", "detail": f"top-level is {type(payload).__name__}"},
            )
            return

        scene_json = payload.get("scene_json")
        if not isinstance(scene_json, dict):
            self._log(
                "canvas_parse_error",
                {"error": "TypeError", "detail": "missing scene_json"},
            )
            return

        # R17 server-side enforcement — a future client that forgets to
        # strip `files` would otherwise leak image data into the snapshot
        # row + the brain prompt. The route schema's `extra="forbid"`
        # would also catch it on the snapshot POST, but we'd rather not
        # ship the bytes that far.
        if "files" in scene_json:
            log.warning("agent.canvas.files_stripped_server_side")
            scene_json = {k: v for k, v in scene_json.items() if k != "files"}

        if not self._allow_canvas_event():
            log.warning(
                "agent.canvas.rate_limited",
                window_s=CANVAS_RATE_LIMIT_WINDOW_S,
                cap=CANVAS_RATE_LIMIT_EVENTS,
            )
            return

        t_ms_raw = payload.get("t_ms")
        t_ms = int(t_ms_raw) if isinstance(t_ms_raw, int | float) else self._now_relative_ms()
        scene_fingerprint = payload.get("scene_fingerprint")

        try:
            parsed_text = parse_scene(scene_json)
        except Exception as exc:
            self._log(
                "canvas_parse_error",
                {"error": exc.__class__.__name__, "detail": str(exc)[:200]},
            )
            log.exception("agent.canvas.parser_error")
            return

        # R23: apply canvas_state to Redis BEFORE dispatching the router
        # event. The brain call inside the router loads SessionState
        # after this CAS, so it typically sees the canvas it's reasoning
        # about. CAS exhaustion is non-fatal — the router writes a
        # canvas_change ledger row regardless, so replay can reconstruct
        # the timeline.
        await self._apply_canvas_state(description=parsed_text, t_ms=t_ms)

        canvas_event = RouterEvent(
            type=EventType.CANVAS_CHANGE,
            t_ms=t_ms,
            payload={
                "scene_text": parsed_text,
                "scene_fingerprint": scene_fingerprint,
            },
            priority=default_priority(EventType.CANVAS_CHANGE),
        )
        # Visible footprint for the happy path so brain.call.begin lines
        # can be correlated with canvas activity. Without this the log
        # looks like the brain wakes up on its own; the actual driver is
        # one CANVAS_CHANGE event per browser publish.
        log.info(
            "agent.canvas.dispatch",
            t_ms=t_ms,
            scene_fingerprint=scene_fingerprint,
            scene_text_len=len(parsed_text),
        )
        try:
            await self._brain.router.handle(canvas_event)
        except Exception:
            log.exception("agent.canvas.router_handle_failed")
            return

        self.schedule_canvas_snapshot(
            self._brain.canvas_snapshot_client.append(
                session_id=self._session_id,
                t_ms=t_ms,
                scene_json=scene_json,
            )
        )

        self._log(
            "canvas_change",
            {
                "t_ms": t_ms,
                "scene_fingerprint": scene_fingerprint,
                "parsed_text": parsed_text,
            },
        )

    def _allow_canvas_event(self) -> bool:
        """Sliding-window rate limit — 60 events / 60 s / session."""
        now = time.monotonic()
        cutoff = now - CANVAS_RATE_LIMIT_WINDOW_S
        # Drop entries older than the window. Linear scan; the list
        # never grows beyond ~60 entries under nominal operation.
        self._canvas_event_history = [t for t in self._canvas_event_history if t > cutoff]
        if len(self._canvas_event_history) >= CANVAS_RATE_LIMIT_EVENTS:
            return False
        self._canvas_event_history.append(now)
        return True

    async def _apply_canvas_state(self, *, description: str, t_ms: int) -> None:
        if self._brain is None:
            return
        store = self._brain.store

        last_change_s = max(0, t_ms // 1000)

        def _mutator(current: SessionState | None) -> SessionState:
            if current is None:
                # No baseline state in Redis (eviction or pre-init); skip
                # — the next brain call will load whatever's there. We
                # intentionally don't synthesize a fresh SessionState
                # here because the canvas description alone isn't enough
                # to seed problem + prompt_version + cost cap.
                raise CanvasNoBaselineStateError("no baseline state for canvas_state apply")
            return current.model_copy(
                update={
                    "canvas_state": CanvasState(
                        description=description, last_change_s=last_change_s
                    ),
                }
            )

        try:
            await store.apply(self._session_id, _mutator)
        except CanvasNoBaselineStateError:
            # Redis key absent (eviction or pre-init) — non-fatal; the
            # canvas_change ledger row + snapshot still land.
            log.warning(
                "agent.canvas.no_baseline_state",
                session_id=str(self._session_id),
                t_ms=t_ms,
            )
        except RedisCasExhaustedError:
            # CAS retry budget exhausted under write contention — non-fatal;
            # the brain may see one-cycle-stale canvas_state for this dispatch.
            log.warning(
                "agent.canvas.cas_exhausted",
                session_id=str(self._session_id),
                t_ms=t_ms,
            )

    async def _append_transcript_turn(self, *, speaker: str, text: str, t_ms: int) -> None:
        """Append a TranscriptTurn to ``state.transcript_window`` via Redis CAS.

        The brain reads the full window on every call (see
        ``brain/prompt_builder.py::build_user_message`` — ``transcript_window``
        ships in the dynamic ``state_json`` blob). Without this hook the
        window stays empty for the life of the session, leaving the brain
        to reason on per-turn payloads only with no cross-turn continuity.

        Mirrors ``_apply_canvas_state`` (R23-style read-after-CAS): the
        caller awaits this before triggering the next brain dispatch so
        the next ``decide(...)`` reads a state that already includes the
        just-emitted turn.

        Capped at ``_TRANSCRIPT_WINDOW_CAP`` turns. Older entries are
        dropped from the head. M4 replaces the hard cap with a Haiku
        session-summary compression cycle (``session_summary`` field
        accumulates compressed history; transcript_window holds the
        recent uncompressed tail).
        """
        if self._brain is None:
            # Static-ack path or brain disabled — no Redis state to update.
            # The transcript_window only matters when the brain reads it.
            return
        store = self._brain.store

        new_turn = TranscriptTurn(t_ms=t_ms, speaker=speaker, text=text)

        def _mutator(current: SessionState | None) -> SessionState:
            if current is None:
                # No baseline state — likely between session-end and
                # session-start, or Redis eviction. The ledger row for
                # this utterance still lands; the next brain call will
                # rebuild state if needed.
                raise TranscriptNoBaselineStateError(
                    "no baseline state for transcript_window apply"
                )
            updated_window = [*current.transcript_window, new_turn]
            if len(updated_window) > _TRANSCRIPT_WINDOW_CAP:
                # Drop oldest. M4 will replace this with summarise-into
                # `session_summary` before discarding.
                updated_window = updated_window[-_TRANSCRIPT_WINDOW_CAP:]
            return current.model_copy(update={"transcript_window": updated_window})

        try:
            await store.apply(self._session_id, _mutator)
        except TranscriptNoBaselineStateError:
            log.warning(
                "agent.transcript.no_baseline_state",
                session_id=str(self._session_id),
                t_ms=t_ms,
                speaker=speaker,
            )
        except RedisCasExhaustedError:
            # Brain may see a one-cycle-stale transcript on the next
            # dispatch; subsequent turns still append cleanly.
            log.warning(
                "agent.transcript.cas_exhausted",
                session_id=str(self._session_id),
                t_ms=t_ms,
                speaker=speaker,
            )

    def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Router → ledger shim.

        The router's `LedgerLogger` protocol passes `dict[str, Any]`;
        `_log` accepts the wider `dict[str, object]`. Exposing the shim
        keeps the type narrowing explicit at the call site rather than
        pushing `cast(...)` into production code.
        """
        self._log(event_type, dict(payload))

    @property
    def session_id(self) -> UUID:
        """Read-only session id for the brain wiring helper."""
        return self._session_id

    def now_relative_ms(self) -> int:
        """Public alias of `_now_relative_ms` so the router closures keep
        bindings that don't start with an underscore (ty's "private from
        outside module" diagnostic fires otherwise)."""
        return self._now_relative_ms()

    def _now_relative_ms(self) -> int:
        """Milliseconds since session start, clamped to a non-negative int.

        `on_enter` is the sole clock initializer. If `_log` is called
        before `on_enter` runs, return 0 rather than seeding `_t0_ms`
        with a different monotonic origin than the session uses.
        """
        if self._t0_ms is None:
            return 0
        return max(0, int(time.monotonic() * 1000) - self._t0_ms)


def build_brain_wiring(
    agent: MentorAgent,
    *,
    brain: BrainClient,
    store: RedisSessionStore,
    snapshot_client: SnapshotClient,
    canvas_snapshot_client: CanvasSnapshotClient,
) -> _BrainWiring:
    """Construct the per-session brain collaborators.

    Separated from `entrypoint` so tests can assemble the same wiring
    against fakes without duplicating router construction. The
    singleton `BrainClient` and `RedisSessionStore` are passed in
    rather than fetched here — tests can pass mock implementations,
    and production's `entrypoint` resolves them via the `get_*`
    helpers before calling.
    """
    queue = UtteranceQueue(agent.now_relative_ms)
    gate = SpeechCheckGate(agent.now_relative_ms)

    router = EventRouter(
        session_id=agent.session_id,
        brain=brain,
        store=store,
        snapshot_client=snapshot_client,
        snapshot_scheduler=agent.schedule_snapshot,
        utterance_queue=queue,
        log_event=agent.log_event,
        now_ms=agent.now_relative_ms,
        synthetic_emitter=agent.emit_synthetic,
        recovery_text=SYNTHETIC_RECOVERY_UTTERANCE,
    )
    return _BrainWiring(
        brain=brain,
        store=store,
        snapshot_client=snapshot_client,
        canvas_snapshot_client=canvas_snapshot_client,
        router=router,
        queue=queue,
        gate=gate,
    )


def prewarm(proc: JobProcess) -> None:
    """Load heavy models once per worker process, before any job runs.

    Without this, the entrypoint's first `AgentSession` instantiation
    triggers Kokoro + whisper model loads (plus NLTK/HF downloads on a
    cold cache) inside the 60-second per-job init watchdog — which
    then kills the process mid-download. Prewarm runs at worker
    startup and has no such deadline.
    """
    # Lazy imports: extras must be installed, but they only need to
    # resolve when a worker is actually prewarming (never in tests/CI).
    from livekit.plugins import silero  # type: ignore[import-not-found]

    from archmentor_agent.audio.framework_adapters import (
        KokoroStreamingTTS,
        WhisperCppSTT,
    )

    settings = get_settings()
    log.info(
        "agent.prewarm.begin",
        whisper_model=settings.whisper_model,
        tts_voice=settings.tts_voice,
        brain_enabled=settings.brain_enabled,
    )
    proc.userdata["vad"] = silero.VAD.load()
    # Construct + eagerly load the whisper model so the first live
    # utterance doesn't pay the cold-start cost. `preload()` was
    # previously implicit via "first call loads"; shipping it as an
    # explicit prewarm step keeps STT latency predictable.
    stt_adapter = WhisperCppSTT()
    stt_adapter.preload()
    proc.userdata["stt"] = stt_adapter
    # Construct Kokoro now so HF weights + NLTK tokenizer load here,
    # not under the job-dispatch watchdog.
    tts = KokoroStreamingTTS()
    # Force the underlying engine to load its voice model eagerly.
    from archmentor_agent.tts import kokoro

    kokoro._load_engine()
    proc.userdata["tts"] = tts
    log.info("agent.prewarm.ready")


async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint — one instance per session dispatch."""
    session_id = _session_id_from_ctx(ctx)
    settings = get_settings()
    api_url, agent_token = _agent_http_config(settings)
    ledger = LedgerClient(LedgerConfig(base_url=api_url, agent_token=agent_token))

    # Snapshot client lives for the duration of the session; shared
    # pool covers the fire-and-forget POST traffic from the router.
    snapshot_client = SnapshotClient(
        SnapshotClientConfig(base_url=api_url, agent_token=agent_token)
    )
    # Canvas-snapshot client is structurally parallel to `snapshot_client`
    # (same auth, same retry policy, same drop-on-4xx semantics). Two
    # clients rather than one shared client because the per-session HTTP
    # connection pool sizing differs by route — canvas events fire
    # ~0-60/min during a session, brain snapshots fire 1 per dispatch.
    canvas_snapshot_client = CanvasSnapshotClient(
        CanvasSnapshotClientConfig(base_url=api_url, agent_token=agent_token)
    )

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=ctx.proc.userdata["stt"],
        tts=ctx.proc.userdata["tts"],
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    log.info(
        "agent.connected",
        room=ctx.room.name,
        session_id=str(session_id),
        brain_enabled=settings.brain_enabled,
    )

    # Fetch the problem card from the API bootstrap route for production
    # sessions. For dev/replay sessions (room name doesn't parse as a
    # `session-<uuid>` room) fall through to `build_dev_problem_card()`.
    # `_fetch_bootstrap_problem` returns None and logs `agent.bootstrap.aborted`
    # when the candidate's R26 keepalive raced ahead of us and the session
    # is already ENDED — in that case we shut down without speaking to an
    # empty room.
    bootstrap_problem, abort_reason = await _fetch_bootstrap_problem(
        session_id=session_id,
        room_name=ctx.room.name or "",
        settings=settings,
    )
    if abort_reason is not None:
        log.info(
            "agent.entrypoint.aborted",
            session_id=str(session_id),
            reason=abort_reason,
        )
        return

    mentor = MentorAgent(
        session_id=session_id,
        ledger=ledger,
        room=ctx.room,
        brain_enabled=settings.brain_enabled,
        brain=None,
        bootstrap_problem=bootstrap_problem,
    )
    if settings.brain_enabled:
        from archmentor_agent.brain.client import get_brain_client
        from archmentor_agent.state.redis_store import get_redis_store

        mentor.attach_brain(
            build_brain_wiring(
                mentor,
                brain=get_brain_client(settings),
                store=get_redis_store(settings),
                snapshot_client=snapshot_client,
                canvas_snapshot_client=canvas_snapshot_client,
            )
        )

    # Keep strong refs to spawned tasks so the GC doesn't cancel them
    # mid-flight (ruff RUF006).
    input_tasks: set[asyncio.Task[None]] = set()
    interim_tasks: set[asyncio.Task[None]] = set()

    def _on_user_input(ev: _UserInputEvent) -> None:
        # Framework emits interim + final transcripts; we react to both.
        is_final = bool(ev.is_final)
        text = (ev.transcript or "").strip()
        log.info(
            "agent.user_input.event",
            is_final=is_final,
            text=text,
            session_id=str(session_id),
        )
        if not is_final:
            if not mentor.opening_complete.is_set() or not text:
                return
            task = asyncio.create_task(mentor.handle_interim_transcript(text))
            interim_tasks.add(task)
            task.add_done_callback(interim_tasks.discard)
            return
        if not mentor.opening_complete.is_set():
            # STT fired during the opening line — almost always whisper
            # hallucinating on the agent's own audio. Drop it.
            log.info("agent.user_input.dropped_pre_intro", text=text)
            return
        task = asyncio.create_task(mentor.handle_user_input(text))
        input_tasks.add(task)
        task.add_done_callback(input_tasks.discard)

    session.on("user_input_transcribed", _on_user_input)

    canvas_tasks: set[asyncio.Task[None]] = set()

    async def _on_canvas_scene(reader: Any, _participant_identity: str) -> None:
        """Receive one `canvas-scene` text-stream message + dispatch.

        livekit-agents 0.20+ exposes `register_text_stream_handler` with
        an async handler signature `(reader, participant_identity)`;
        `reader.read_all()` yields the full assembled text. Older / newer
        SDK variants may rename — adjust the import or the handler shape
        if the SDK shifts.
        """
        try:
            raw = await reader.read_all()
        except Exception:
            log.exception("agent.canvas.read_failed")
            return
        await mentor.on_canvas_scene_payload(raw)

    def _on_canvas_scene_sync(reader: Any, participant_identity: str) -> None:
        task = asyncio.create_task(_on_canvas_scene(reader, participant_identity))
        canvas_tasks.add(task)
        task.add_done_callback(canvas_tasks.discard)

    # Register only when the brain is enabled; the kill-switch path has
    # nowhere to dispatch canvas events to.
    if settings.brain_enabled:
        try:
            ctx.room.register_text_stream_handler(CANVAS_SCENE_TOPIC, _on_canvas_scene_sync)
        except Exception:
            log.exception("agent.canvas.handler_registration_failed")

    # `AgentSession.wait_for_inactive()` returns the moment the speech
    # queue is idle AND the user hasn't spoken yet — which is true for
    # the ~1ms gap between `session.start()` returning and `on_enter`'s
    # `say()` scheduling the opening utterance. The finally block then
    # runs `aclose()` and the intro's `say()` raises "AgentSession is
    # closing". Wait instead on the real room lifecycle: either the
    # agent's own room connection drops, or the last remote participant
    # (i.e. the candidate) leaves.
    room_closed = asyncio.Event()

    def _mark_closed(*_: object) -> None:
        room_closed.set()

    def _on_participant_left(participant: rtc.RemoteParticipant) -> None:
        log.info("agent.participant_disconnected", identity=participant.identity)
        if not ctx.room.remote_participants:
            room_closed.set()

    ctx.room.on("disconnected", _mark_closed)
    ctx.room.on("participant_disconnected", _on_participant_left)

    try:
        await session.start(room=ctx.room, agent=mentor)
        await room_closed.wait()
        log.info("agent.room_closed")
    finally:
        # Close the session first so no new user_input_transcribed
        # events get dispatched (and no new `input_tasks` are created).
        await session.aclose()
        # Drain any in-flight handle_user_input + handle_interim tasks.
        # STT can produce a final transcript up to a second after the
        # user disconnects, and both handlers schedule fire-and-forget
        # work (ledger writes, snapshot posts) whose lifecycle outlives
        # the coroutine that started them.
        if input_tasks:
            log.info("agent.shutdown.drain_input_tasks", count=len(input_tasks))
            await asyncio.gather(*input_tasks, return_exceptions=True)
        if interim_tasks:
            log.info("agent.shutdown.drain_interim_tasks", count=len(interim_tasks))
            await asyncio.gather(*interim_tasks, return_exceptions=True)
        # Drain text-stream read tasks BEFORE mentor.shutdown() so any
        # in-flight reader.read_all() + on_canvas_scene_payload body
        # finishes before the router and ledger close. These are the
        # Task[None] tasks spawned by _on_canvas_scene_sync; they are
        # distinct from mentor._canvas_tasks (Task[bool] snapshot POSTs)
        # which shutdown() drains internally.
        if canvas_tasks:
            log.info("agent.shutdown.drain_canvas_tasks", count=len(canvas_tasks))
            await asyncio.gather(*canvas_tasks, return_exceptions=True)
        # MentorAgent.shutdown() drains the router, snapshot tasks, and
        # ledger tasks in the correct order, then deletes the Redis
        # session key. Keep this call idempotent — `_brain is None` on
        # the kill-switch path, in which case only ledger drains run.
        await mentor.shutdown()
        await snapshot_client.aclose()
        await canvas_snapshot_client.aclose()
        await ledger.aclose()


async def _fetch_bootstrap_problem(
    *,
    session_id: UUID,
    room_name: str,
    settings: Settings,
) -> tuple[ProblemCard | None, str | None]:
    """Fetch the ProblemCard from the control-plane bootstrap API.

    Returns `(problem, abort_reason)`:
    - `(problem, None)` — bootstrap succeeded; agent should proceed.
    - `(None, None)` — falling back to `build_dev_problem_card()` (dev/replay
      room name, brain disabled, or transient fetch error).
    - `(None, abort_reason)` — agent should shut down without speaking. This
      fires when the candidate's R26 keepalive Fetch raced ahead of the
      worker's bootstrap and the session is already ENDED — speaking the
      opening utterance would TTS into an empty room and burn Anthropic
      budget on a dead session.

    Logs structured events so operators can distinguish production
    bootstraps, dev-path fallbacks, and tab-close-aborts.
    """
    if not settings.brain_enabled:
        return None, None

    # Dev/replay sessions use raw UUIDs or non-session-prefixed room names.
    if not room_name.startswith("session-"):
        log.info(
            "agent.bootstrap.dev_fallback",
            reason="non_session_room",
            room_name=room_name,
        )
        return None, None

    try:
        bootstrap = await fetch_session_bootstrap(
            api_url=settings.api_url,
            agent_token=settings.agent_ingest_token.get_secret_value(),
            session_id=session_id,
        )
    except BootstrapFetchError as exc:
        # Log loudly but don't crash the session — fall back to dev card
        # so the voice loop can still run (the brain will use the dev
        # problem, which is wrong but recoverable for a dev session).
        # In production this should page the operator.
        log.error(
            "agent.bootstrap.fetch_failed",
            session_id=str(session_id),
            error=str(exc),
        )
        log.info(
            "agent.bootstrap.dev_fallback",
            reason="fetch_error",
            session_id=str(session_id),
        )
        return None, None

    if bootstrap.status != "active":
        # Candidate's R26 keepalive Fetch (POST /sessions/{id}/end on
        # `beforeunload`) won the race against our boot. Don't speak —
        # there's nobody to hear it, and /events would 409 anyway.
        log.warning(
            "agent.bootstrap.aborted",
            session_id=str(session_id),
            session_status=bootstrap.status,
            reason="session_not_active_at_bootstrap",
        )
        return None, "session_not_active_at_bootstrap"

    log.info(
        "agent.bootstrap.fetched",
        session_id=str(session_id),
        problem_slug=bootstrap.problem_slug,
    )
    return (
        ProblemCard(
            slug=bootstrap.problem_slug,
            # API doesn't expose version or title via the bootstrap route yet;
            # use defaults that won't affect the brain prompt (the brain reads
            # statement_md and rubric_yaml, not slug/version/title).
            version=1,
            title=bootstrap.problem_slug,
            statement_md=bootstrap.statement_md,
            rubric_yaml=bootstrap.rubric_yaml,
        ),
        None,
    )


def _session_id_from_ctx(ctx: JobContext) -> UUID:
    """Extract the session UUID from the room name.

    Rooms are named `session-<uuid>` by the control plane. For M1 dev
    flows we also accept a raw UUID. Fails loudly if absent — we must
    never write events against a fake session id.
    """
    room = ctx.room.name or ""
    candidate = room.removeprefix("session-")
    try:
        return UUID(candidate)
    except ValueError as exc:
        raise RuntimeError(
            f"Cannot extract session UUID from LiveKit room name {room!r}. "
            "Expected `session-<uuid>` (control-plane convention) or a bare UUID."
        ) from exc


def _agent_http_config(settings: Settings | None = None) -> tuple[str, str]:
    """Return ``(api_url, agent_ingest_token)`` from ``Settings``.

    All three ingest clients (ledger, snapshot, canvas-snapshot) share the
    same trust boundary and the same token, so a single helper avoids
    three near-identical builders. Each construction site builds its own
    typed config dataclass from the returned tuple.

    Passing ``settings`` explicitly lets the entrypoint reuse the object
    it already resolved; the default path falls back to the cached singleton
    so tests that call this function directly still work.
    """
    cfg = settings or get_settings()
    return cfg.api_url, cfg.agent_ingest_token.get_secret_value()


# Re-exposed for tests that only need a fresh `PendingUtterance`.
__all__ = [
    "AI_STATE_TOPIC",
    "OPENING_UTTERANCE",
    "TURN_ACK_UTTERANCE",
    "MentorAgent",
    "PendingUtterance",
    "_agent_http_config",
    "build_initial_session_state",
    "entrypoint",
    "main",
    "prewarm",
]


def main() -> None:
    # The livekit-agents CLI reads LIVEKIT_URL / LIVEKIT_API_KEY /
    # LIVEKIT_API_SECRET directly from os.environ. Unlike the API (which
    # gets .env via pydantic-settings), the agent has no framework-level
    # dotenv loader — load it here, anchored at the repo root.
    #
    # `override=True` is dev-only: iterating on .env (e.g., switching
    # whisper models) silently fails if a stale shell export still has
    # the old value. In production the orchestrator injects env vars
    # directly; overriding them with a stale on-disk .env would be a
    # silent credential-swap hazard. ARCHMENTOR_ENV=dev (the default)
    # enables override; any other value runs with shell-wins semantics.
    repo_root = Path(__file__).resolve().parents[3]
    env_name = os.environ.get("ARCHMENTOR_ENV", "dev")
    load_dotenv(repo_root / ".env", override=(env_name == "dev"))

    # The Claude Code sandbox sets ALL_PROXY=socks5h://... for outbound
    # traffic, which aiohttp/httpx honour for *every* connection —
    # including ws://localhost, even though NO_PROXY lists localhost.
    # That routes the LiveKit control-plane websocket through the
    # SOCKS proxy and produces a 400 handshake. Drop the proxies here
    # so local-dev traffic goes direct; model warm-up has already run
    # via scripts/warm_models.py.
    for _var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        os.environ.pop(_var, None)
        os.environ.pop(_var.lower(), None)

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # Give the worker a generous model-load budget. Default is
            # 10s, which isn't enough for Kokoro + spaCy + NLTK cold
            # start even on warm HF cache.
            initialize_process_timeout=300.0,
        )
    )


if __name__ == "__main__":
    main()
