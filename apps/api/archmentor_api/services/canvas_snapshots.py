"""Full Excalidraw scene snapshot write-through.

Mirrors `services/snapshots.append_snapshot` deliberately — same
insert-only discipline, same caller-owns-transaction contract. Kept
structurally parallel so the two ingest paths don't drift; if one
gains a check (size cap, schema guard) the other should too.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlmodel import Session

from archmentor_api.models.canvas_snapshot import CanvasSnapshot


def append_canvas_snapshot(
    db: Session,
    *,
    session_id: UUID,
    t_ms: int,
    scene_json: dict[str, Any],
) -> CanvasSnapshot:
    """Insert one canvas snapshot row and return it with the generated id."""
    if t_ms < 0:
        raise ValueError("t_ms must be non-negative (ms since session start)")

    row = CanvasSnapshot(
        session_id=session_id,
        t_ms=t_ms,
        # Defensive copy — payload is insert-only and the caller's dict
        # may be mutated further (e.g. if the agent's canvas handler is
        # already building the next scene update).
        scene_json=dict(scene_json),
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return row
