"""LiveKit token minting.

The browser calls this endpoint with a Supabase JWT to receive a
short-lived LiveKit room token scoped to a single room. The token
identity is the Supabase `user_id`; grants allow publish + subscribe
for the one room only.

Room ownership is enforced by looking the requested room up in the
`sessions` table: the caller's `user_id` must match the session's
`user_id`, and the session must be `ACTIVE`. Without this check any
authenticated user could mint a token for any room belonging to any
other user's session.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from livekit import api
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from archmentor_api.config import Settings, get_settings
from archmentor_api.db import get_db_session
from archmentor_api.deps import CurrentUser
from archmentor_api.models.session import InterviewSession, SessionStatus

router = APIRouter(prefix="/livekit", tags=["livekit"])


class TokenRequest(BaseModel):
    room: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str


def _session_for_room(db: Session, room: str) -> InterviewSession | None:
    """Return the session whose `livekit_room` matches, if any."""
    return db.exec(select(InterviewSession).where(InterviewSession.livekit_room == room)).first()


@router.post("/token", status_code=status.HTTP_201_CREATED)
def mint_token(
    body: TokenRequest,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[Session, Depends(get_db_session)],
) -> TokenResponse:
    session_row = _session_for_room(db, body.room)
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No session for this room",
        )
    # Compare as strings: `Principal.user_id` is a JWT `sub` (string);
    # `InterviewSession.user_id` is a UUID column.
    if str(session_row.user_id) != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not your session",
        )
    if session_row.status is not SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is not active",
        )

    grants = api.VideoGrants(
        room_join=True,
        room=body.room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    token = (
        api.AccessToken(
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
        .with_identity(user.user_id)
        .with_name(user.email or user.user_id)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=settings.livekit_token_ttl_s))
        .to_jwt()
    )
    return TokenResponse(
        token=token,
        url=settings.livekit_url,
        room=body.room,
        identity=user.user_id,
    )
