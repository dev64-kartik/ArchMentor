"""Session lifecycle + agent ingest.

User-facing endpoints (create/list/get/end/delete) call into
`services.sessions`; agent ingest endpoints (events, snapshots) live
beside them so the body-size middleware and `_require_active_session`
gate stay one read away.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from archmentor_api.config import Settings, get_settings
from archmentor_api.db import get_db_session
from archmentor_api.deps import CurrentUser, require_agent
from archmentor_api.models.problem import Problem
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.session_event import SessionEventType
from archmentor_api.services.canvas_snapshots import append_canvas_snapshot
from archmentor_api.services.event_ledger import append_event
from archmentor_api.services.sessions import create_session, get_owned_session
from archmentor_api.services.snapshots import append_snapshot

router = APIRouter(prefix="/sessions", tags=["sessions"])

# Hard cap on the serialized `payload_json` body to protect the
# append-only ledger (and, in M2, the brain's rolling transcript) from
# an adversarial or runaway agent sending multi-megabyte blobs. 16 KiB
# is ~16x the largest realistic single-turn transcript at 300 wpm and
# leaves headroom for brain-decision payloads. Ingest that approaches
# this cap in practice is itself a signal worth investigating.
_MAX_PAYLOAD_JSON_BYTES = 16 * 1024

# Snapshots are larger than events by construction — the full
# `SessionState` plus Opus reasoning text can run tens of KiB. 256 KiB
# covers realistic 45-minute sessions (rolling transcript capped at
# 2-3 minutes, decisions log worst-case < ~10 KiB) with headroom; any
# single snapshot row approaching this cap is a signal the state model
# is leaking history that should be compressed.
_MAX_SNAPSHOT_PAYLOAD_BYTES = 256 * 1024


def _require_active_session(db: Session, session_id: UUID) -> InterviewSession:
    """Fetch + validate a session row for agent ingest, taking a row lock.

    404 if missing, 409 if not ACTIVE. Both the events and snapshots
    routes need the identical check; hoisting it keeps the two routes
    structurally parallel — a drift here would create an asymmetric
    trust boundary between events and snapshots, which is exactly the
    kind of bug that survives review by being "obvious."

    Uses `SELECT ... FOR UPDATE` so the same-transaction insert that
    follows is protected against a concurrent `POST /sessions/{id}/end`
    flipping the row to ENDED between this gate and the INSERT — the
    classic TOCTOU window. SQLite silently ignores FOR UPDATE; the test
    harness still exercises the code path even though the lock is a
    no-op there. On Postgres the row lock holds until commit.
    """
    session_row = db.exec(
        select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
    ).first()
    if session_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session_row.status is not SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active (status={session_row.status.value})",
        )
    return session_row


class ProblemRef(BaseModel):
    slug: str
    version: int
    title: str
    difficulty: str


class SessionView(BaseModel):
    """Response shape for create/list/get/end.

    Mirrors the columns the frontend's session dashboard + LiveKit join
    flow need; intentionally omits cost columns + token totals (replay
    + eval read those directly from Postgres).
    """

    session_id: UUID
    livekit_room: str
    livekit_url: str
    status: SessionStatus
    started_at: datetime | None
    ended_at: datetime | None
    problem: ProblemRef


def _session_to_view(row: InterviewSession, problem: Problem, livekit_url: str) -> SessionView:
    """Build a `SessionView` from a pre-fetched session row + problem."""
    return SessionView(
        session_id=row.id,
        livekit_room=row.livekit_room,
        livekit_url=livekit_url,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
        problem=ProblemRef(
            slug=problem.slug,
            version=problem.version,
            title=problem.title,
            difficulty=problem.difficulty,
        ),
    )


def _build_view(db: Session, row: InterviewSession, livekit_url: str) -> SessionView:
    problem = db.get(Problem, row.problem_id)
    if problem is None:
        # Defensive: a session row exists with an FK to a problem that's
        # been deleted. Surface as 500 — manual intervention needed.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session references a missing problem",
        )
    return _session_to_view(row, problem, livekit_url)


def _user_uuid(user: CurrentUser) -> UUID:
    """Coerce the JWT subject string into a UUID for FK comparisons."""
    try:
        return UUID(user.user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject is not a valid user id",
        ) from exc


class CreateSessionBody(BaseModel):
    problem_slug: str = Field(min_length=1, max_length=100)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SessionView)
@router.post(
    "/", status_code=status.HTTP_201_CREATED, response_model=SessionView, include_in_schema=False
)
def create_session_endpoint(
    body: CreateSessionBody,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionView:
    user_id = _user_uuid(user)
    row = create_session(db, user_id=user_id, problem_slug=body.problem_slug)
    db.commit()
    db.refresh(row)
    return _build_view(db, row, settings.livekit_url)


@router.get("", response_model=list[SessionView])
@router.get("/", response_model=list[SessionView], include_in_schema=False)
def list_sessions(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[SessionView]:
    user_id = _user_uuid(user)
    # Fetch all sessions in one query, then batch-fetch their Problems in a
    # single IN-clause query — 2 total queries regardless of session count,
    # avoiding the N+1 that results from calling db.get(Problem, ...) per row.
    rows = db.exec(
        select(InterviewSession)
        .where(InterviewSession.user_id == user_id)
        # ty sees the field's Python type (datetime | None) rather than the
        # SQLAlchemy column descriptor; the runtime InstrumentedAttribute
        # is what `sa.desc` actually receives.
        .order_by(sa.desc(InterviewSession.started_at))  # ty: ignore[invalid-argument-type]
    ).all()
    if not rows:
        return []
    problem_ids = list({row.problem_id for row in rows})
    problems = db.exec(select(Problem).where(Problem.id.in_(problem_ids))).all()  # ty: ignore[unresolved-attribute]
    problem_index = {p.id: p for p in problems}
    views: list[SessionView] = []
    for row in rows:
        problem = problem_index.get(row.problem_id)
        if problem is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Session references a missing problem",
            )
        views.append(_session_to_view(row, problem, settings.livekit_url))
    return views


@router.get("/{session_id}", response_model=SessionView)
def get_session(
    session_id: UUID,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionView:
    user_id = _user_uuid(user)
    row = get_owned_session(db, session_id=session_id, user_id=user_id)
    return _build_view(db, row, settings.livekit_url)


@router.post("/{session_id}/end", response_model=SessionView)
def end_session(
    session_id: UUID,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionView:
    """Flip an ACTIVE session to ENDED.

    The LiveKit room is NOT closed here; closing it mid-session would
    force-disconnect the candidate's mic before the agent's closing
    utterance plays. The agent's room-emptied callback handles cleanup.
    """
    user_id = _user_uuid(user)
    # Take the row lock first so a racing /events ingest sees the new
    # status under the same TOCTOU contract as `_require_active_session`.
    locked = db.exec(
        select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
    ).first()
    if locked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if locked.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session")
    if locked.status is SessionStatus.ENDED:
        # Idempotent: already ended — return the current row without re-writing.
        # Callers that call /end twice (network retry, browser unload race) should
        # not get a 409; the session is in the desired terminal state.
        return _build_view(db, locked, settings.livekit_url)
    if locked.status is not SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active (status={locked.status.value})",
        )

    locked.status = SessionStatus.ENDED
    locked.ended_at = datetime.now(UTC)
    db.add(locked)
    db.commit()
    db.refresh(locked)
    return _build_view(db, locked, settings.livekit_url)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: UUID,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db_session)],
) -> None:
    """Hard-delete the session; child rows cascade via Postgres ON DELETE CASCADE.

    Requires the session to be in a non-ACTIVE state. Call POST /end first.
    """
    user_id = _user_uuid(user)
    row = get_owned_session(db, session_id=session_id, user_id=user_id)
    if row.status is SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is active; call /end before deleting",
        )
    db.delete(row)
    db.commit()


class SessionBootstrap(BaseModel):
    """Bootstrap payload for the agent worker on session connect.

    Carries everything the agent needs to seed the brain's ProblemCard
    without a separate DB query — problem content is stable for the
    session lifetime, so fetching it once at startup is the right contract.
    """

    session_id: UUID
    status: SessionStatus
    problem_slug: str
    statement_md: str
    rubric_yaml: str


@router.get(
    "/{session_id}/bootstrap",
    response_model=SessionBootstrap,
    dependencies=[Depends(require_agent)],
)
def get_session_bootstrap(
    session_id: UUID,
    db: Annotated[Session, Depends(get_db_session)],
) -> SessionBootstrap:
    """Return the problem content the agent worker needs at session start.

    Authenticated via X-Agent-Token (agent-only). Status-agnostic by design:
    the agent worker is dispatched on LiveKit room creation and races the
    candidate's tab-close keepalive Fetch (R26). If the candidate closes
    the tab before the worker finishes booting, the session is already
    ENDED when the bootstrap fetch arrives — but the problem content is
    read-only and stable, so returning it is harmless. The `status` field
    lets the agent decide whether to proceed with TTS/brain init or shut
    down cleanly without speaking to an empty room.
    """
    session_row = db.get(InterviewSession, session_id)
    if session_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    problem = db.get(Problem, session_row.problem_id)
    if problem is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session references a missing problem",
        )
    return SessionBootstrap(
        session_id=session_id,
        status=session_row.status,
        problem_slug=problem.slug,
        statement_md=problem.statement_md,
        rubric_yaml=problem.rubric_yaml,
    )


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
    payload_bytes = len(json.dumps(body.payload_json))
    if payload_bytes > _MAX_PAYLOAD_JSON_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"payload_json too large: {payload_bytes} bytes (max {_MAX_PAYLOAD_JSON_BYTES})"
            ),
        )

    _require_active_session(db, session_id)

    event = append_event(
        db,
        session_id=session_id,
        t_ms=body.t_ms,
        event_type=body.type,
        payload=body.payload_json,
    )
    db.commit()
    return AppendEventResponse(id=event.id, t_ms=event.t_ms, type=event.type)


class AppendSnapshotBody(BaseModel):
    """Request body for `POST /sessions/{id}/snapshots`.

    Mirrors the `BrainSnapshot` SQLModel columns 1:1 so the agent's
    snapshot builder (`archmentor_agent.snapshots.serializer`) can
    emit the dict shape directly without per-field mapping. Token
    counts are validated here (not just at the DB) so a negative
    value returns 422 before the transaction opens.
    """

    t_ms: int = Field(ge=0, description="Milliseconds since session start")
    session_state_json: dict[str, object] = Field(default_factory=dict)
    event_payload_json: dict[str, object] = Field(default_factory=dict)
    brain_output_json: dict[str, object] = Field(default_factory=dict)
    reasoning_text: str = Field(default="")
    tokens_input: int = Field(ge=0, default=0)
    tokens_output: int = Field(ge=0, default=0)


class AppendSnapshotResponse(BaseModel):
    id: UUID
    t_ms: int


@router.post(
    "/{session_id}/snapshots",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent)],
)
def append_brain_snapshot(
    session_id: UUID,
    body: AppendSnapshotBody,
    db: Annotated[Session, Depends(get_db_session)],
) -> AppendSnapshotResponse:
    """Append a brain snapshot row.

    Same auth + session-active semantics as `/events`, larger payload
    cap (256 KiB) because a snapshot carries the full `SessionState`
    + Opus reasoning text. No GET endpoint is exposed by design —
    snapshots are write-only from the agent; replay reads them
    directly from Postgres via `scripts/replay.py` with DB creds.
    """
    # Aggregate byte check across the full snapshot body. Computing this
    # once via model_dump_json() avoids four separate encode calls and
    # accurately reflects the total payload size including multi-byte
    # Unicode (e.g. Hinglish transcript paths). The body-size middleware
    # is the primary gate; this check is defense-in-depth in case a
    # future router refactor bypasses the middleware.
    total_bytes = len(body.model_dump_json().encode("utf-8"))
    if total_bytes > _MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"snapshot payload too large: {total_bytes} bytes "
                f"(max {_MAX_SNAPSHOT_PAYLOAD_BYTES})"
            ),
        )

    _require_active_session(db, session_id)

    snapshot = append_snapshot(
        db,
        session_id=session_id,
        t_ms=body.t_ms,
        session_state_json=body.session_state_json,
        event_payload_json=body.event_payload_json,
        brain_output_json=body.brain_output_json,
        reasoning_text=body.reasoning_text,
        tokens_input=body.tokens_input,
        tokens_output=body.tokens_output,
    )
    db.commit()
    return AppendSnapshotResponse(id=snapshot.id, t_ms=snapshot.t_ms)


class AppendCanvasSnapshotBody(BaseModel):
    """Request body for `POST /sessions/{id}/canvas-snapshots`.

    `extra="forbid"` enforces R17 server-side: the agent already strips
    `files` from the scene, but a future client (replay harness, second
    whiteboard) that forgets to do so will get a 422 here instead of
    silently leaking image data into the database. Same gate at the
    schema level so the protection is structural, not a code path the
    handler can accidentally drop.
    """

    model_config = ConfigDict(extra="forbid")

    t_ms: int = Field(ge=0, description="Milliseconds since session start")
    scene_json: dict[str, object] = Field(default_factory=dict)

    @field_validator("scene_json", mode="before")
    @classmethod
    def _reject_nested_files(cls, v: Any) -> Any:
        """R17: scene_json must not carry a 'files' key.

        Excalidraw embeds image data under `files`; the agent strips it before
        publishing, but a future client that forgets to do so must be caught
        structurally rather than silently persisting binary data.
        """
        if isinstance(v, dict) and "files" in v:
            raise ValueError("scene_json must not contain a 'files' key (R17)")
        return v


@router.post(
    "/{session_id}/canvas-snapshots",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agent)],
)
def append_canvas_snapshot_endpoint(
    session_id: UUID,
    body: AppendCanvasSnapshotBody,
    db: Annotated[Session, Depends(get_db_session)],
) -> AppendSnapshotResponse:
    """Append a full Excalidraw scene snapshot row.

    Same auth + active-session semantics as `/snapshots`. Body-size cap
    (256 KiB) lives on the middleware; the in-handler check below is
    defense-in-depth for the JSON blob (the middleware caps the entire
    request body, the in-handler cap re-measures the parsed scene_json
    in case headers misreport).

    Schema explicitly forbids `files` per R17 — image data must not
    cross the API boundary.
    """
    # Acquire the FOR UPDATE row lock first (same order as brain-snapshot
    # route) so the size check runs inside the same transaction that will
    # INSERT, preventing a TOCTOU window between the active-session gate
    # and the INSERT.
    _require_active_session(db, session_id)

    scene_bytes = len(json.dumps(body.scene_json).encode("utf-8"))
    if scene_bytes > _MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"scene_json too large: {scene_bytes} bytes (max {_MAX_SNAPSHOT_PAYLOAD_BYTES})"
            ),
        )

    snapshot = append_canvas_snapshot(
        db,
        session_id=session_id,
        t_ms=body.t_ms,
        scene_json=body.scene_json,
    )
    db.commit()
    return AppendSnapshotResponse(id=snapshot.id, t_ms=snapshot.t_ms)
