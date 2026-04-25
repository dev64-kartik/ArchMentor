"""Session lifecycle + agent ingest.

User-facing endpoints (list/create/get/end/delete) are M2; only the
agent ingest endpoints are live in M1/M2.
"""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from archmentor_api.db import get_db_session
from archmentor_api.deps import CurrentUser, require_agent
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.session_event import SessionEventType
from archmentor_api.services.event_ledger import append_event
from archmentor_api.services.snapshots import append_snapshot

router = APIRouter(prefix="/sessions", tags=["sessions"])

# Hard cap on the serialized `payload_json` body to protect the
# append-only ledger (and, in M2, the brain's rolling transcript) from
# an adversarial or runaway agent sending multi-megabyte blobs. 16 KiB
# is ~16x the largest realistic single-turn transcript at 300 wpm and
# leaves headroom for brain-decision payloads. Ingest that approaches
# this cap in practice is itself a signal worth investigating.
_MAX_PAYLOAD_JSON_BYTES = 16 * 1024

# Snapshots are larger than events by construction — the full
# `SessionState` plus Opus reasoning text can run tens of KiB. 256 KiB
# covers realistic 45-minute sessions (rolling transcript capped at
# 2-3 minutes, decisions log worst-case < ~10 KiB) with headroom; any
# single snapshot row approaching this cap is a signal the state model
# is leaking history that should be compressed.
_MAX_SNAPSHOT_PAYLOAD_BYTES = 256 * 1024


def _require_active_session(db: Session, session_id: UUID) -> InterviewSession:
    """Fetch + validate a session row for agent ingest, taking a row lock.

    404 if missing, 409 if not ACTIVE. Both the events and snapshots
    routes need the identical check; hoisting it keeps the two routes
    structurally parallel — a drift here would create an asymmetric
    trust boundary between events and snapshots, which is exactly the
    kind of bug that survives review by being "obvious."

    Uses `SELECT ... FOR UPDATE` so the same-transaction insert that
    follows is protected against a concurrent `POST /sessions/{id}/end`
    flipping the row to ENDED between this gate and the INSERT — the
    classic TOCTOU window. SQLite silently ignores FOR UPDATE; the test
    harness still exercises the code path even though the lock is a
    no-op there. On Postgres the row lock holds until commit.
    """
    session_row = db.exec(
        select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
    ).first()
    if session_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session_row.status is not SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active (status={session_row.status.value})",
        )
    return session_row


@router.get("/")
def list_sessions(user: CurrentUser) -> list[dict[str, object]]:
    # TODO(M2): query `sessions` for this user_id.
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_session(user: CurrentUser) -> dict[str, object]:
    # TODO(M2): create session row, mint LiveKit token, dispatch agent worker.
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("/{session_id}")
def get_session(session_id: UUID, user: CurrentUser) -> dict[str, object]:
    _ = user
    _ = session_id
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.post("/{session_id}/end")
def end_session(session_id: UUID, user: CurrentUser) -> dict[str, object]:
    _ = user
    _ = session_id
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: UUID, user: CurrentUser) -> None:
    _ = user
    _ = session_id
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


class AppendEventBody(BaseModel):
    t_ms: int = Field(ge=0, description="Milliseconds since session start")
    type: SessionEventType
    payload_json: dict[str, object] = Field(default_factory=dict)


class AppendEventResponse(BaseModel):
    id: UUID
    t_ms: int
    type: SessionEventType


@router.post(
    "/{session_id}/events",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent)],
)
def append_session_event(
    session_id: UUID,
    body: AppendEventBody,
    db: Annotated[Session, Depends(get_db_session)],
) -> AppendEventResponse:
    """Append a single event to the session ledger.

    Called by the LiveKit agent worker via shared-secret auth. Never
    mutates existing rows.
    """
    # JSON serialization length check; cheap and sufficient for our
    # DoS / injection surface concern. Pydantic's `dict[str, object]`
    # gives us no structural bound by default.
    payload_bytes = len(json.dumps(body.payload_json))
    if payload_bytes > _MAX_PAYLOAD_JSON_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"payload_json too large: {payload_bytes} bytes (max {_MAX_PAYLOAD_JSON_BYTES})"
            ),
        )

    _require_active_session(db, session_id)

    event = append_event(
        db,
        session_id=session_id,
        t_ms=body.t_ms,
        event_type=body.type,
        payload=body.payload_json,
    )
    db.commit()
    return AppendEventResponse(id=event.id, t_ms=event.t_ms, type=event.type)


class AppendSnapshotBody(BaseModel):
    """Request body for `POST /sessions/{id}/snapshots`.

    Mirrors the `BrainSnapshot` SQLModel columns 1:1 so the agent's
    snapshot builder (`archmentor_agent.snapshots.serializer`) can
    emit the dict shape directly without per-field mapping. Token
    counts are validated here (not just at the DB) so a negative
    value returns 422 before the transaction opens.
    """

    t_ms: int = Field(ge=0, description="Milliseconds since session start")
    session_state_json: dict[str, object] = Field(default_factory=dict)
    event_payload_json: dict[str, object] = Field(default_factory=dict)
    brain_output_json: dict[str, object] = Field(default_factory=dict)
    reasoning_text: str = Field(default="")
    tokens_input: int = Field(ge=0, default=0)
    tokens_output: int = Field(ge=0, default=0)


class AppendSnapshotResponse(BaseModel):
    id: UUID
    t_ms: int


@router.post(
    "/{session_id}/snapshots",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent)],
)
def append_brain_snapshot(
    session_id: UUID,
    body: AppendSnapshotBody,
    db: Annotated[Session, Depends(get_db_session)],
) -> AppendSnapshotResponse:
    """Append a brain snapshot row.

    Same auth + session-active semantics as `/events`, larger payload
    cap (256 KiB) because a snapshot carries the full `SessionState`
    + Opus reasoning text. No GET endpoint is exposed by design —
    snapshots are write-only from the agent; replay reads them
    directly from Postgres via `scripts/replay.py` with DB creds.
    """
    # Aggregate byte check across all four JSON blobs + reasoning text.
    # A session_state_json that's narrowly under cap but combined with
    # a reasoning_text blob pushes total storage over — checking the
    # sum is what actually bounds the row size at rest. All four
    # measurements use UTF-8 byte length so multi-byte Unicode (e.g.
    # the Hinglish transcript path) is counted accurately instead of
    # being undercounted by `len(str)`.
    total_bytes = (
        len(json.dumps(body.session_state_json).encode("utf-8"))
        + len(json.dumps(body.event_payload_json).encode("utf-8"))
        + len(json.dumps(body.brain_output_json).encode("utf-8"))
        + len(body.reasoning_text.encode("utf-8"))
    )
    if total_bytes > _MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"snapshot payload too large: {total_bytes} bytes "
                f"(max {_MAX_SNAPSHOT_PAYLOAD_BYTES})"
            ),
        )

    _require_active_session(db, session_id)

    snapshot = append_snapshot(
        db,
        session_id=session_id,
        t_ms=body.t_ms,
        session_state_json=body.session_state_json,
        event_payload_json=body.event_payload_json,
        brain_output_json=body.brain_output_json,
        reasoning_text=body.reasoning_text,
        tokens_input=body.tokens_input,
        tokens_output=body.tokens_output,
    )
    db.commit()
    return AppendSnapshotResponse(id=snapshot.id, t_ms=snapshot.t_ms)
