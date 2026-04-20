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
from archmentor_api.models.session import InterviewSession
from archmentor_api.models.session_event import SessionEventType
from archmentor_api.services.event_ledger import append_event

router = APIRouter(prefix="/sessions", tags=["sessions"])


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
    session_row = db.get(InterviewSession, session_id)
    if session_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    event = append_event(
        db,
        session_id=session_id,
        t_ms=body.t_ms,
        event_type=body.type,
        payload=body.payload_json,
    )
    db.commit()
    return AppendEventResponse(id=event.id, t_ms=event.t_ms, type=event.type)
