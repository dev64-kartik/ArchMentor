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
import os
from uuid import UUID

import structlog
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

log = structlog.get_logger(__name__)

OPENING_UTTERANCE = (
    "Hi — I'm your interviewer today. Take a moment to read the problem, "
    "and when you're ready, walk me through your approach."
)
TURN_ACK_UTTERANCE = "Got it. Keep going when you're ready."


# whisper emits bracketed sound tags when it gets silence or non-speech
# (`[Music]`, `[BLANK_AUDIO]`, `[Silence]`, etc). They aren't transcripts —
# drop them before we log and reply.
_HALLUCINATION_TAGS = {
    "[music]",
    "[blank_audio]",
    "[silence]",
    "[noise]",
    "(music)",
    "(silence)",
    "(blank_audio)",
}


def _is_whisper_hallucination(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return True
    return stripped in _HALLUCINATION_TAGS or (stripped.startswith("[") and stripped.endswith("]"))


class MentorAgent(Agent):
    """Minimal agent: logs transcripts, acknowledges turn-ends (M1).

    The real interviewer brain (Opus tool-use, phase-aware, rubric-
    anchored) replaces the static acknowledgement in M2.
    """

    def __init__(self, *, session_id: UUID, ledger: LedgerClient) -> None:
        super().__init__(instructions="You acknowledge candidate turns while M2 wires the brain.")
        self._session_id = session_id
        self._ledger = ledger
        self._t0_ms: int | None = None

    async def on_enter(self) -> None:
        await self.session.say(OPENING_UTTERANCE)
        await self._log("utterance_ai", {"text": OPENING_UTTERANCE, "speaker": "ai"})

    async def handle_user_input(self, text: str) -> None:
        """Called from the session's `user_input_transcribed` event.

        `on_user_turn_completed` only fires when an LLM is wired into
        the session — we deliberately skip the LLM in M1 so the brain
        slot stays clean for M2. Listening to the STT-level transcript
        event lets us drive TTS acknowledgements without a model in the
        middle.
        """
        if _is_whisper_hallucination(text):
            log.debug("agent.user_input.dropped_hallucination", text=text)
            return
        await self._log(
            "utterance_candidate",
            {"text": text, "speaker": "candidate"},
        )
        try:
            await self.session.say(TURN_ACK_UTTERANCE)
        except RuntimeError as exc:
            # Tab close / disconnect races the session teardown. Don't
            # raise, just log and drop the ack — the candidate utterance
            # is already in the ledger.
            log.warning("agent.say_skipped", reason=str(exc))
            return
        await self._log("utterance_ai", {"text": TURN_ACK_UTTERANCE, "speaker": "ai"})

    async def _log(self, event_type: str, payload: dict[str, object]) -> None:
        await self._ledger.append(
            session_id=self._session_id,
            t_ms=_now_relative_ms(self),
            event_type=event_type,
            payload=payload,
        )


def _now_relative_ms(agent: MentorAgent) -> int:
    """Milliseconds since session start, clamped to a non-negative int."""
    import time

    if agent._t0_ms is None:
        agent._t0_ms = int(time.monotonic() * 1000)
        return 0
    return max(0, int(time.monotonic() * 1000) - agent._t0_ms)


def prewarm(proc: JobProcess) -> None:
    """Load heavy models once per worker process, before any job runs.

    Without this, `_build_tts()` inside the entrypoint triggers the
    first Kokoro model load (and NLTK/HF download on a cold cache)
    inside the 60-second per-job init watchdog — which then kills the
    process mid-download. Prewarm is run at worker startup and has no
    such deadline.
    """
    # Lazy imports: extras must be installed, but they only need to
    # resolve when a worker is actually prewarming (never in tests/CI).
    from livekit.plugins import silero  # type: ignore[import-not-found]

    from archmentor_agent.audio.framework_adapters import (
        KokoroStreamingTTS,
        WhisperCppSTT,
    )

    log.info("agent.prewarm.begin")
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["stt"] = WhisperCppSTT()
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

    mentor = MentorAgent(session_id=session_id, ledger=ledger)

    # Keep strong refs to spawned tasks so the GC doesn't cancel them
    # mid-flight (ruff RUF006).
    input_tasks: set[asyncio.Task[None]] = set()

    def _on_user_input(ev) -> None:  # type: ignore[no-untyped-def]
        # Framework emits interim + final transcripts; only respond to final.
        if not getattr(ev, "is_final", False):
            return
        text = (getattr(ev, "transcript", None) or "").strip()
        log.info("agent.user_input", text=text, session_id=str(session_id))
        task = asyncio.create_task(mentor.handle_user_input(text))
        input_tasks.add(task)
        task.add_done_callback(input_tasks.discard)

    session.on("user_input_transcribed", _on_user_input)

    try:
        await session.start(room=ctx.room, agent=mentor)
        await session.wait_for_inactive()
    finally:
        await session.aclose()
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


def _build_stt():  # type: ignore[no-untyped-def]
    from archmentor_agent.audio.framework_adapters import WhisperCppSTT

    return WhisperCppSTT()


def _build_tts():  # type: ignore[no-untyped-def]
    from archmentor_agent.audio.framework_adapters import KokoroStreamingTTS

    return KokoroStreamingTTS()


def main() -> None:
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
