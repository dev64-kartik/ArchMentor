"""Serialize a brain decision into a replayable snapshot.

Every brain call writes one snapshot row to `brain_snapshots` so the
eval harness can replay a historical decision through a current prompt
and diff the output (ghost diff).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from archmentor_agent.state import SessionState


def build_snapshot(
    *,
    session_id: UUID,
    t_ms: int,
    state: SessionState,
    event_payload: dict[str, Any],
    brain_output: dict[str, Any],
    reasoning: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
) -> dict[str, Any]:
    """Return the row payload for a `brain_snapshots` insert.

    Keys match the `BrainSnapshot` SQLModel in the API so `scripts/replay.py`
    can round-trip the snapshot through either storage layer without
    transformation.
    """
    if t_ms < 0:
        raise ValueError("t_ms must be non-negative (ms since session start)")
    if tokens_input < 0 or tokens_output < 0:
        raise ValueError("token counts must be non-negative")

    return {
        "session_id": str(session_id),
        "t_ms": t_ms,
        "session_state_json": state.model_dump(mode="json"),
        "event_payload_json": dict(event_payload),
        "brain_output_json": dict(brain_output),
        "reasoning_text": reasoning,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
    }
