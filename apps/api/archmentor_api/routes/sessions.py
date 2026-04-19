"""Session lifecycle (stubs)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from archmentor_api.deps import CurrentUser

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/")
def list_sessions(user: CurrentUser) -> list[dict[str, object]]:
    # TODO(M2): query `sessions` for this user_id.
    _ = user
    return []


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_session(user: CurrentUser) -> dict[str, object]:
    # TODO(M2): create session row, mint LiveKit token, dispatch agent worker.
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("/{session_id}")
def get_session(session_id: UUID, user: CurrentUser) -> dict[str, object]:
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.post("/{session_id}/end")
def end_session(session_id: UUID, user: CurrentUser) -> dict[str, object]:
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: UUID, user: CurrentUser) -> None:
    _ = user
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")
