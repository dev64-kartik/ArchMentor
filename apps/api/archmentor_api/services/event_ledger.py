"""Append-only event ledger.

Every observable session event (candidate utterance, AI utterance, brain
decision, canvas diff, phase transition, etc.) is written here. Writes
are insert-only; never mutate existing rows. This is the foundation for
replay, eval harness, and all session analytics.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlmodel import Session

from archmentor_api.models.session_event import SessionEvent, SessionEventType


def append_event(
    db: Session,
    *,
    session_id: UUID,
    t_ms: int,
    event_type: SessionEventType,
    payload: dict[str, Any],
) -> SessionEvent:
    """Insert a single event into `session_events` and return the row.

    The caller owns the transaction boundary (commit or rollback). This
    function only stages the insert + flushes so the row's generated `id`
    and `created_at` are visible to the caller.
    """
    if t_ms < 0:
        raise ValueError("t_ms must be non-negative (ms since session start)")

    event = SessionEvent(
        session_id=session_id,
        t_ms=t_ms,
        type=event_type,
        payload_json=dict(payload),  # defensive copy; payload is insert-only
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    return event
