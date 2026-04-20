"""LiveKit token minting.

The browser calls this endpoint with a Supabase JWT to receive a
short-lived LiveKit room token scoped to a single room. The token
identity is the Supabase `user_id`; grants allow publish + subscribe
for the one room only.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from livekit import api
from pydantic import BaseModel, Field

from archmentor_api.config import Settings, get_settings
from archmentor_api.deps import CurrentUser

router = APIRouter(prefix="/livekit", tags=["livekit"])


class TokenRequest(BaseModel):
    room: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str


@router.post("/token")
def mint_token(
    body: TokenRequest,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
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
