"""LiveKit agent worker entrypoint.

Responsibilities in M1:
- Join the LiveKit room dispatched to this worker.
- Speak a static opening line via `session.say()`.
- Detect candidate turn-ends via the framework's built-in VAD + STT.
- Append `utterance_candidate` / `utterance_ai` events to the control-
  plane event ledger over HTTP.
- Keep pre-VAD noise gating in-path so keyboard/trackpad clicks don't
  fire false turn-ends.

Brain wiring (tool-use brain, phase state, counter-argument, etc.)
lands in M2. For now the agent responds with a static acknowledgement
at each turn-end — enough to prove the voice loop end-to-end.

Audio deps (`pywhispercpp`, `streaming-tts`) are optional; this module
imports cleanly in CI where those wheels aren't installed. The import
of the real STT/TTS adapters happens only when `entrypoint` is called.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Literal, Protocol
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

from archmentor_agent.ledger import LedgerClient, LedgerConfig

AiState = Literal["speaking", "listening", "thinking"]
AI_STATE_TOPIC = "ai_state"


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
    "and when you're ready, walk me through your approach."
)
TURN_ACK_UTTERANCE = "Got it. Keep going when you're ready."


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


def _is_whisper_hallucination(text: str) -> bool:
    stripped = text.strip().lower().rstrip(".!?,")
    if not stripped:
        return True
    if stripped in _HALLUCINATION_TAGS or (stripped.startswith("[") and stripped.endswith("]")):
        return True
    return any(phrase in stripped for phrase in _HALLUCINATION_PHRASES)


class MentorAgent(Agent):
    """Minimal agent: logs transcripts, acknowledges turn-ends (M1).

    The real interviewer brain (Opus tool-use, phase-aware, rubric-
    anchored) replaces the static acknowledgement in M2.
    """

    def __init__(self, *, session_id: UUID, ledger: LedgerClient, room: rtc.Room) -> None:
        super().__init__(instructions="You acknowledge candidate turns while M2 wires the brain.")
        self._session_id = session_id
        self._ledger = ledger
        self._room = room
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

    async def on_enter(self) -> None:
        log.info("agent.on_enter.begin", session_id=str(self._session_id))
        self._t0_ms = int(time.monotonic() * 1000)
        # `opening_complete` must be set no matter what. If we leave it
        # unset (TTS error, ledger error, cancellation), the STT event
        # handler drops every user turn for the life of the session —
        # the session is alive and connected but effectively deaf.
        # Surface the error, then unblock STT in `finally`.
        try:
            await self._publish_state("speaking")
            log.info("agent.opening.say.begin", text=OPENING_UTTERANCE)
            handle = self.session.say(OPENING_UTTERANCE)
            await handle.wait_for_playout()
            log.info("agent.opening.say.end")
            self._log("utterance_ai", {"text": OPENING_UTTERANCE, "speaker": "ai"})
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

    async def handle_user_input(self, text: str) -> None:
        """Called from the session's `user_input_transcribed` event.

        `on_user_turn_completed` only fires when an LLM is wired into
        the session — we deliberately skip the LLM in M1 so the brain
        slot stays clean for M2. Listening to the STT-level transcript
        event lets us drive TTS acknowledgements without a model in the
        middle.
        """
        if _is_whisper_hallucination(text):
            log.info("agent.user_input.dropped_hallucination", text=text)
            return
        await self._publish_state("thinking")
        self._log(
            "utterance_candidate",
            {"text": text, "speaker": "candidate"},
        )
        log.info("agent.ack.begin", ack=TURN_ACK_UTTERANCE)
        await self._publish_state("speaking")
        try:
            await self.session.say(TURN_ACK_UTTERANCE)
        except RuntimeError as exc:
            # Tab close / disconnect races the session teardown. Don't
            # raise, just log and drop the ack — the candidate utterance
            # is already in the ledger.
            log.warning("agent.say_skipped", reason=str(exc))
            await self._publish_state("listening")
            return
        log.info("agent.ack.end")
        self._log("utterance_ai", {"text": TURN_ACK_UTTERANCE, "speaker": "ai"})
        await self._publish_state("listening")

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
        except (ConnectionError, OSError, RuntimeError) as exc:
            # Data publish must never break the voice loop — room is
            # mid-teardown or the participant isn't connected yet.
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

    def _now_relative_ms(self) -> int:
        """Milliseconds since session start, clamped to a non-negative int.

        `on_enter` is the sole clock initializer. If `_log` is called
        before `on_enter` runs, return 0 rather than seeding `_t0_ms`
        with a different monotonic origin than the session uses.
        """
        if self._t0_ms is None:
            return 0
        return max(0, int(time.monotonic() * 1000) - self._t0_ms)


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

    whisper_model = os.environ.get("ARCHMENTOR_WHISPER_MODEL", "large-v3")
    tts_voice = os.environ.get("ARCHMENTOR_TTS_VOICE", "af_bella")
    log.info("agent.prewarm.begin", whisper_model=whisper_model, tts_voice=tts_voice)
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
    ledger = LedgerClient(_ledger_config())

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=ctx.proc.userdata["stt"],
        tts=ctx.proc.userdata["tts"],
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    log.info("agent.connected", room=ctx.room.name, session_id=str(session_id))

    mentor = MentorAgent(session_id=session_id, ledger=ledger, room=ctx.room)

    # Keep strong refs to spawned tasks so the GC doesn't cancel them
    # mid-flight (ruff RUF006).
    input_tasks: set[asyncio.Task[None]] = set()

    def _on_user_input(ev: _UserInputEvent) -> None:
        # Framework emits interim + final transcripts; only respond to final.
        is_final = bool(ev.is_final)
        text = (ev.transcript or "").strip()
        log.info(
            "agent.user_input.event",
            is_final=is_final,
            text=text,
            session_id=str(session_id),
        )
        if not is_final:
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
        # Drain any in-flight handle_user_input tasks. STT can produce
        # a final transcript up to a second after the user disconnects,
        # and handle_user_input schedules fire-and-forget ledger writes
        # whose lifecycle outlives the coroutine that started them.
        if input_tasks:
            log.info("agent.shutdown.drain_input_tasks", count=len(input_tasks))
            await asyncio.gather(*input_tasks, return_exceptions=True)
        # Now drain the ledger writes themselves — these tasks were
        # scheduled from within handle_user_input (and on_enter) and
        # can still be mid-request even after handle_user_input has
        # returned. Finishing them before closing the httpx client
        # prevents `client has been closed` errors and lost events.
        if mentor._ledger_tasks:
            log.info(
                "agent.shutdown.drain_ledger_tasks",
                count=len(mentor._ledger_tasks),
            )
            await asyncio.gather(*mentor._ledger_tasks, return_exceptions=True)
        await ledger.aclose()


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


def _ledger_config() -> LedgerConfig:
    base = os.environ.get("ARCHMENTOR_API_URL", "http://localhost:8000")
    token = os.environ.get("ARCHMENTOR_AGENT_INGEST_TOKEN")
    if not token:
        raise RuntimeError("ARCHMENTOR_AGENT_INGEST_TOKEN is required")
    return LedgerConfig(base_url=base, agent_token=token)


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
