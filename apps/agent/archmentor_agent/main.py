"""LiveKit agent worker entry point.

Wiring target (M1):

    async def entrypoint(ctx: JobContext):
        session = AgentSession(vad=silero.VAD.load())

        @ctx.room.on("track_subscribed")
        def on_track(track, *_):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(stt_pipeline(track))

        @ctx.room.on("text_received")
        def on_canvas(message, participant):
            if message.topic == "canvas-diff":
                router.handle_canvas_change(json.loads(message.text))

        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        await router.speak_opening()

    if __name__ == "__main__":
        cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

This file is a scaffold. Implementation lands in M1.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def entrypoint() -> None:
    """Placeholder entrypoint. Replaced by livekit-agents JobContext handler in M1."""
    log.info("archmentor-agent entrypoint invoked (scaffold)")


def main() -> None:
    """CLI entry. Real wiring uses `livekit.agents.cli.run_app(WorkerOptions(...))`."""
    log.info("archmentor-agent scaffold — no worker registered yet")


if __name__ == "__main__":
    main()
