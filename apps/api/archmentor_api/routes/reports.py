"""Report retrieval (stubs)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from archmentor_api.deps import CurrentUser

router = APIRouter(prefix="/sessions/{session_id}/report", tags=["reports"])


@router.get("/")
def get_report(session_id: UUID, user: CurrentUser) -> dict[str, object]:
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")
