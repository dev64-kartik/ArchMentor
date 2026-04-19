"""Problem catalog (stubs)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/problems", tags=["problems"])


@router.get("/")
def list_problems() -> list[dict[str, object]]:
    # TODO(M3/M6): read from `problems` table seeded by scripts/seed_problems.py
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("/{slug}")
def get_problem(slug: str) -> dict[str, object]:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Problem '{slug}' not found")
