"""Problem catalog read endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from archmentor_api.db import get_db_session
from archmentor_api.deps import CurrentUser
from archmentor_api.models.problem import Problem

router = APIRouter(prefix="/problems", tags=["problems"])


class ProblemSummary(BaseModel):
    """List-view payload for the `/session/new` problem picker.

    Heavy fields (`statement_md`, `rubric_yaml`, `ideal_solution_md`,
    `seniority_calibration_json`) are intentionally absent — they're
    only needed when the candidate selects a problem and we hit
    `GET /problems/{slug}`.
    """

    slug: str
    version: int
    title: str
    difficulty: str


class ProblemDetail(ProblemSummary):
    statement_md: str
    rubric_yaml: str


@router.get("", response_model=list[ProblemSummary])
@router.get("/", response_model=list[ProblemSummary], include_in_schema=False)
def list_problems(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db_session)],
) -> list[Problem]:
    _ = user
    return list(db.exec(select(Problem).order_by(Problem.slug)).all())


@router.get("/{slug}", response_model=ProblemDetail)
def get_problem(
    slug: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db_session)],
) -> Problem:
    _ = user
    row = db.exec(select(Problem).where(Problem.slug == slug)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Problem '{slug}' not found",
        )
    return row
