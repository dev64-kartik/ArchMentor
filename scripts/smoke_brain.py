"""Manual smoke harness for the M2 brain loop.

Runs three scripted candidate turns through the real `BrainClient`
against the URL-shortener dev problem. Asserts that each call returns
a tool_use response (stop_reason == "tool_use") and that at least one
of the three decisions is `speak` — otherwise the brain has silently
regressed into an always-silent mode, which looks identical to a
working session from the voice-loop side.

This is a **manual validation aid**, not a unit test. It needs a real
`ARCHMENTOR_ANTHROPIC_API_KEY` and burns three Opus calls per run.
Don't wire it into CI.

Usage:
    ARCHMENTOR_ANTHROPIC_API_KEY=sk-ant-... uv run python scripts/smoke_brain.py
"""
# ruff: noqa: T201  # manual smoke test — print is the UX

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from archmentor_agent.brain.bootstrap import (
    DEV_PROMPT_VERSION,
    build_dev_problem_card,
)
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision
from archmentor_agent.config import get_settings
from archmentor_agent.state.session_state import (
    InterviewPhase,
    SessionState,
    TranscriptTurn,
)

_SCRIPTED_TURNS: tuple[tuple[int, str, str], ...] = (
    (
        0,
        "candidate",
        "I'd use a 7-character base62 code. The hash function would take the "
        "long URL and the user id, hash it with SHA-256, and take the first 7 "
        "bytes encoded as base62.",
    ),
    (
        45_000,
        "candidate",
        "Wait actually for sizing — 100M writes a month is 40 per second. "
        "Storage is 100M times maybe 500 bytes, so 50 GB per month. At a "
        "5-year horizon that's 3 terabytes. Reads are 10B per month so "
        "about 4000 per second average, peak maybe 40000.",
    ),
    (
        90_000,
        "candidate",
        "For the redirect path I'd put a Redis cache in front of the DB. "
        "Cache misses go to Postgres, cache hits skip the DB entirely. "
        "I think the cache hit rate would be like 99% because most "
        "short URLs are created once and never re-fetched.",
    ),
)


def _build_state_after(turn_index: int) -> SessionState:
    """SessionState at the point just after turn `turn_index` lands.

    The brain is called *after* the candidate finishes a turn, so for
    turn N the state already includes turns 0..N in its rolling
    transcript. Mirrors the live router's behavior.
    """
    transcript: list[TranscriptTurn] = []
    for t_ms, speaker, text in _SCRIPTED_TURNS[: turn_index + 1]:
        transcript.append(TranscriptTurn(t_ms=t_ms, speaker=speaker, text=text))
    return SessionState(
        problem=build_dev_problem_card(),
        system_prompt_version=DEV_PROMPT_VERSION,
        started_at=datetime.now(UTC),
        elapsed_s=_SCRIPTED_TURNS[turn_index][0] // 1_000,
        remaining_s=max(0, 2700 - _SCRIPTED_TURNS[turn_index][0] // 1_000),
        phase=InterviewPhase.REQUIREMENTS,
        transcript_window=transcript,
    )


async def _run_turn(brain: BrainClient, turn_index: int) -> BrainDecision:
    state = _build_state_after(turn_index)
    t_ms = _SCRIPTED_TURNS[turn_index][0]
    event = {"type": "turn_end", "t_ms": t_ms, "text": _SCRIPTED_TURNS[turn_index][2]}
    return await brain.decide(state=state, event=event, t_ms=t_ms)


async def _main_async() -> int:
    settings = get_settings()
    if "replace_with_" in settings.anthropic_api_key.get_secret_value():
        print(
            "ARCHMENTOR_ANTHROPIC_API_KEY is the .env.example placeholder. "
            "Export a real key and retry.",
            file=sys.stderr,
        )
        return 2
    brain = BrainClient(settings)
    try:
        decisions: list[BrainDecision] = []
        for idx in range(len(_SCRIPTED_TURNS)):
            print(f"--- turn {idx + 1}/{len(_SCRIPTED_TURNS)} ---")
            decision = await _run_turn(brain, idx)
            decisions.append(decision)
            print(
                f"  decision={decision.decision} priority={decision.priority} "
                f"confidence={decision.confidence:.2f} reason={decision.reason!r}"
            )
            if decision.utterance:
                print(f"  utterance: {decision.utterance}")
    finally:
        await brain.aclose()

    speaks = sum(1 for d in decisions if d.decision == "speak")
    if speaks == 0:
        print("! no `speak` decisions across 3 turns — brain is stuck silent", file=sys.stderr)
        return 1
    print(f"\n{speaks}/{len(decisions)} decisions emitted `speak` — brain loop OK")
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    sys.exit(main())
