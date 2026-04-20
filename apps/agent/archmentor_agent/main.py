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

import os
from uuid import UUID

import structlog
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
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

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:  # type: ignore[no-untyped-def]
        text = _turn_text(new_message)
        if text:
            await self._log(
                "utterance_candidate",
                {"text": text, "speaker": "candidate"},
            )
        await self.session.say(TURN_ACK_UTTERANCE)
        await self._log("utterance_ai", {"text": TURN_ACK_UTTERANCE, "speaker": "ai"})

    async def _log(self, event_type: str, payload: dict[str, object]) -> None:
        await self._ledger.append(
            session_id=self._session_id,
            t_ms=_now_relative_ms(self),
            event_type=event_type,
            payload=payload,
        )


def _turn_text(new_message) -> str:  # type: ignore[no-untyped-def]
    """Extract plain text from a framework ChatMessage."""
    content = getattr(new_message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(p) for p in content if isinstance(p, str)]
        return " ".join(parts).strip()
    return ""


def _now_relative_ms(agent: MentorAgent) -> int:
    """Milliseconds since session start, clamped to a non-negative int."""
    import time

    if agent._t0_ms is None:
        agent._t0_ms = int(time.monotonic() * 1000)
        return 0
    return max(0, int(time.monotonic() * 1000) - agent._t0_ms)


async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint — one instance per session dispatch."""
    session_id = _session_id_from_ctx(ctx)
    ledger = LedgerClient(_ledger_config())

    # Lazy imports keep the module loadable in CI without audio extras.
    from livekit.plugins import silero  # type: ignore[import-not-found]

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=_build_stt(),
        tts=_build_tts(),
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    log.info("agent.connected", room=ctx.room.name, session_id=str(session_id))

    try:
        await session.start(
            room=ctx.room,
            agent=MentorAgent(session_id=session_id, ledger=ledger),
        )
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
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    main()
