"""Seed a deterministic user + problem + session for M1 mic smoke tests.

Idempotent: running it multiple times updates in place. Creates:
- User  id = 00000000-0000-0000-0000-0000000000AA
- Problem slug = dev-test
- Session id = 00000000-0000-0000-0000-000000000001
  livekit_room = `session-00000000-0000-0000-0000-000000000001`

The LiveKit agent worker extracts `00000000-0000-0000-0000-000000000001`
from the room name and appends transcript events against that session
id. Tail them with:

    docker compose -f infra/docker-compose.yml exec postgres \\
      psql -U postgres -d archmentor -c \\
      "SELECT t_ms, type, payload_json FROM session_events \\
       WHERE session_id = '00000000-0000-0000-0000-000000000001' \\
       ORDER BY t_ms;"
"""

from __future__ import annotations

from uuid import UUID

import archmentor_api.models  # noqa: F401 — register tables on metadata
from archmentor_api.db import engine
from archmentor_api.models.problem import Problem
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.user import User
from sqlmodel import Session, select

DEV_USER_ID = UUID("000000000000000000000000000000aa")
DEV_SESSION_ID = UUID("00000000-0000-0000-0000-000000000001")
DEV_PROBLEM_SLUG = "dev-test"
DEV_ROOM = f"session-{DEV_SESSION_ID}"


def main() -> None:
    with Session(engine) as db:
        user = db.get(User, DEV_USER_ID)
        if user is None:
            user = User(id=DEV_USER_ID, email="dev@archmentor.local")
            db.add(user)

        problem = db.exec(select(Problem).where(Problem.slug == DEV_PROBLEM_SLUG)).first()
        if problem is None:
            problem = Problem(
                slug=DEV_PROBLEM_SLUG,
                title="Dev-only smoke test problem",
                statement_md="# Smoke test\n\nSay anything — this problem exists only so the "
                "M1 voice loop has a session row to append events against.",
                difficulty="easy",
                rubric_yaml="dimensions: []",
                ideal_solution_md="(n/a)",
                seniority_calibration_json={},
            )
            db.add(problem)
        db.flush()

        session_row = db.get(InterviewSession, DEV_SESSION_ID)
        if session_row is None:
            session_row = InterviewSession(
                id=DEV_SESSION_ID,
                user_id=user.id,
                problem_id=problem.id,
                problem_version=problem.version,
                status=SessionStatus.ACTIVE,
                livekit_room=DEV_ROOM,
                prompt_version="m1-dev",
            )
            db.add(session_row)
        else:
            session_row.status = SessionStatus.ACTIVE
            session_row.livekit_room = DEV_ROOM

        db.commit()

    print(f"seeded dev session {DEV_SESSION_ID} (room {DEV_ROOM})")  # noqa: T201


if __name__ == "__main__":
    main()
