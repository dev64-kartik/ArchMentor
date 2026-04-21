"""Session lifecycle + agent ingest.

User-facing endpoints (list/create/get/end/delete) are M2; only the
agent ingest endpoint is live in M1.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from archmentor_api.db import get_db_session
from archmentor_api.deps import CurrentUser, require_agent
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.session_event import SessionEventType
from archmentor_api.services.event_ledger import append_event

router = APIRouter(prefix="/sessions", tags=["sessions"])

# Hard cap on the serialized `payload_json` body to protect the
# append-only ledger (and, in M2, the brain's rolling transcript) from
# an adversarial or runaway agent sending multi-megabyte blobs. 16 KiB
# is ~16x the largest realistic single-turn transcript at 300 wpm and
# leaves headroom for brain-decision payloads. Ingest that approaches
# this cap in practice is itself a signal worth investigating.
_MAX_PAYLOAD_JSON_BYTES = 16 * 1024


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
    import json as _json

    payload_bytes = len(_json.dumps(body.payload_json))
    if payload_bytes > _MAX_PAYLOAD_JSON_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"payload_json too large: {payload_bytes} bytes (max {_MAX_PAYLOAD_JSON_BYTES})"
            ),
        )

    session_row = db.get(InterviewSession, session_id)
    if session_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    # Writes to an ended/errored session poison replay and eval state.
    # Reject with 409 so the agent can distinguish a hanging stale
    # dispatch from a routing error.
    if session_row.status is not SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active (status={session_row.status.value})",
        )

    event = append_event(
        db,
        session_id=session_id,
        t_ms=body.t_ms,
        event_type=body.type,
        payload=body.payload_json,
    )
    db.commit()
    return AppendEventResponse(id=event.id, t_ms=event.t_ms, type=event.type)
