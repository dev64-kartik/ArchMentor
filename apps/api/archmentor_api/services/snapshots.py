"""Brain snapshot write-through.

Every brain call serializes full `SessionState` + event payload + brain
output + reasoning + token counts into one `brain_snapshots` row. This
is the artifact `scripts/replay.py` reads to re-run a historical
decision through a current prompt and diff the output.

Mirrors `services/event_ledger.append_event` deliberately — writes are
insert-only, the caller owns the transaction, and payload shape is
trusted (the route handler runs the size/integrity checks). Keeping the
two services structurally parallel makes it obvious if one drifts.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlmodel import Session

from archmentor_api.models.brain_snapshot import BrainSnapshot


def append_snapshot(
    db: Session,
    *,
    session_id: UUID,
    t_ms: int,
    session_state_json: dict[str, Any],
    event_payload_json: dict[str, Any],
    brain_output_json: dict[str, Any],
    reasoning_text: str,
    tokens_input: int,
    tokens_output: int,
) -> BrainSnapshot:
    """Insert one snapshot row and return it with the generated id."""
    if t_ms < 0:
        raise ValueError("t_ms must be non-negative (ms since session start)")
    if tokens_input < 0 or tokens_output < 0:
        raise ValueError("token counts must be non-negative")

    row = BrainSnapshot(
        session_id=session_id,
        t_ms=t_ms,
        # Defensive copies — payload is insert-only and the caller's dict
        # may be mutated further (e.g. if the router is still building
        # the next event payload on the same reference).
        session_state_json=dict(session_state_json),
        event_payload_json=dict(event_payload_json),
        brain_output_json=dict(brain_output_json),
        reasoning_text=reasoning_text,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return row
