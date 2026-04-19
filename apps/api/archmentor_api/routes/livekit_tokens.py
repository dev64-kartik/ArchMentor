"""LiveKit token minting (stub)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from archmentor_api.deps import CurrentUser

router = APIRouter(prefix="/livekit", tags=["livekit"])


@router.post("/token")
def mint_token(user: CurrentUser) -> dict[str, str]:
    # TODO(M1): mint a LiveKit room token scoped to `user.user_id` with 15m TTL.
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")
