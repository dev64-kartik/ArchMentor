"""Seed a deterministic problem + session for M1 mic smoke tests.

Idempotent: running it multiple times updates in place.

    # Default — seeds a synthetic `dev@archmentor.local` owner. Useful
    # for headless replay tests that never hit /livekit/token.
    uv run python scripts/seed_dev_session.py

    # Re-own the dev session to whoever you're signing in as. Without
    # this, /livekit/token returns 403 "Not your session" because the
    # JWT sub doesn't match the seeded owner.
    uv run python scripts/seed_dev_session.py --email you@example.com

Creates / updates:
- Problem slug = dev-test
- Session id = 00000000-0000-0000-0000-000000000001
  livekit_room = `session-00000000-0000-0000-0000-000000000001`

The LiveKit agent worker extracts `00000000-0000-0000-0000-000000000001`
from the room name and appends transcript events against that session
id. Tail them with:

    docker compose -f infra/docker-compose.yml --env-file .env \\
      exec postgres psql -U postgres -d archmentor -c \\
      "SELECT t_ms, type, payload_json FROM session_events \\
       WHERE session_id = '00000000-0000-0000-0000-000000000001' \\
       ORDER BY t_ms;"
"""

from __future__ import annotations

import argparse
from uuid import UUID

import archmentor_api.models  # noqa: F401 — register tables on metadata
from archmentor_api.config import get_settings
from archmentor_api.db import engine
from archmentor_api.models.problem import Problem
from archmentor_api.models.session import InterviewSession, SessionStatus
from archmentor_api.models.user import User
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlmodel import Session, select

DEV_USER_ID = UUID("000000000000000000000000000000aa")
DEV_USER_EMAIL = "dev@archmentor.local"
DEV_SESSION_ID = UUID("00000000-0000-0000-0000-000000000001")
DEV_PROBLEM_SLUG = "dev-test"
DEV_ROOM = f"session-{DEV_SESSION_ID}"


def _resolve_owner(email: str | None) -> tuple[UUID, str | None]:
    """Return `(user_id, email)` for the session owner.

    With no `email` arg, returns the synthetic dev user — matches the
    original seed behaviour for headless replay tests that never hit
    `/livekit/token`. With `--email`, looks the row up in the separate
    `auth` database so the dev session ends up owned by whoever is
    actually signing in via GoTrue.
    """
    if email is None:
        return DEV_USER_ID, DEV_USER_EMAIL

    # API_DATABASE_URL points at the `archmentor` DB; GoTrue lives in
    # `auth` on the same Postgres. Swap the database name rather than
    # adding a second env var that can drift out of sync.
    # Pass the URL object directly — `str(URL)` masks the password as
    # `***` and the engine would then try to authenticate with the
    # literal string "***".
    auth_url = make_url(get_settings().database_url).set(database="auth")
    auth_engine = create_engine(auth_url)
    try:
        with auth_engine.connect() as conn:
            row = conn.execute(
                text("SELECT id FROM auth.users WHERE email = :email"),
                {"email": email},
            ).one_or_none()
    finally:
        auth_engine.dispose()

    if row is None:
        raise SystemExit(
            f"✗ no GoTrue user with email {email!r}. "
            "Sign up via the web app first (http://localhost:3000)."
        )
    return row[0], email


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a dev session for M1 mic smoke tests.")
    parser.add_argument(
        "--email",
        help="GoTrue user email to own the dev session. "
        "Omit to use the synthetic dev@archmentor.local user.",
    )
    args = parser.parse_args()

    owner_id, owner_email = _resolve_owner(args.email)

    with Session(engine) as db:
        user = db.get(User, owner_id)
        if user is None:
            user = User(id=owner_id, email=owner_email)
            db.add(user)
        elif owner_email and user.email != owner_email:
            user.email = owner_email

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
            # Re-own on every run so swapping --email actually takes
            # effect; otherwise the /livekit/token check keeps 403-ing.
            session_row.user_id = user.id
            session_row.status = SessionStatus.ACTIVE
            session_row.livekit_room = DEV_ROOM

        db.commit()

    print(  # noqa: T201
        f"seeded dev session {DEV_SESSION_ID} (room {DEV_ROOM}, owner {owner_id} <{owner_email}>)"
    )


if __name__ == "__main__":
    main()
