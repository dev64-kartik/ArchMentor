"""Session lifecycle helpers.

Centralizes session-row construction and ownership checks so route handlers
stay focused on HTTP concerns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from archmentor_api.models.problem import Problem
from archmentor_api.models.session import InterviewSession, SessionStatus

# Tracks which prompt revision was active when the session started. Stored
# on the row so replay/eval can correlate decisions with prompt content.
# Bump in lock-step with `apps/agent/archmentor_agent/brain/bootstrap.py`'s
# `DEV_PROMPT_VERSION`; the API is the source of truth at session creation
# because the agent reads it from the DB row.
DEFAULT_PROMPT_VERSION = "m3-canvas"

DEFAULT_COST_CAP_USD = 5.0
DEFAULT_DURATION_S_PLANNED = 2700  # 45 minutes


def _utcnow() -> datetime:
    return datetime.now(UTC)


def create_session(
    db: Session,
    *,
    user_id: UUID,
    problem_slug: str,
) -> InterviewSession:
    """Create an ACTIVE session row for the given user + problem slug.

    Raises 422 if the slug doesn't resolve. The caller owns the
    transaction boundary.
    """
    problem = db.exec(select(Problem).where(Problem.slug == problem_slug)).first()
    if problem is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown problem_slug: {problem_slug!r}",
        )

    row = InterviewSession(
        user_id=user_id,
        problem_id=problem.id,
        problem_version=problem.version,
        status=SessionStatus.ACTIVE,
        started_at=_utcnow(),
        duration_s_planned=DEFAULT_DURATION_S_PLANNED,
        # Placeholder until the row's id is generated; rewritten below.
        livekit_room="pending",
        prompt_version=DEFAULT_PROMPT_VERSION,
        cost_cap_usd=DEFAULT_COST_CAP_USD,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    # Room name binds 1:1 to session id so the LiveKit-token route can
    # resolve a room back to its owning session without an extra column.
    row.livekit_room = f"session-{row.id}"
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


def get_owned_session(
    db: Session,
    *,
    session_id: UUID,
    user_id: UUID,
) -> InterviewSession:
    """Fetch a session by id, enforcing ownership.

    404 if missing, 403 if not owned by the caller.

    # Intentional 403/404 split (see ce:review 2026-04-26 finding #8):
    # 404 for missing rows, 403 for cross-user. Allows operators to
    # distinguish a typo from a wrong-tenant URL. Session UUIDs are 122-bit
    # random so the enumeration oracle is not practically exploitable.
    # Project precedent: /livekit/token uses the same split.
    # Revisit if session_ids ever appear in user-shareable URLs that could
    # leak across tenants.
    """
    row = db.get(InterviewSession, session_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session")
    return row
