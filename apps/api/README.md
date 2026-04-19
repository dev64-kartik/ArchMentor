# archmentor-api

FastAPI control-plane for ArchMentor.

## Stack

- FastAPI + Pydantic v2 + SQLModel
- Alembic (migrations in `migrations/versions`)
- Postgres via `psycopg[binary]`
- Redis via `redis-py`
- JWT verification via `PyJWT` (shared secret with Supabase Auth/GoTrue)

## Commands

```bash
# From repo root
uv sync                                                            # install workspace deps
uv run --package archmentor-api uvicorn archmentor_api.main:app --reload

# Lint / typecheck / test
uv run ruff check apps/api
uv run ty check apps/api
uv run pytest apps/api

# Migrations (from apps/api/)
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
```

## Routes

- `GET /health` — liveness
- `GET /me` — authed echo of Supabase JWT claims (M0 verify target)
- `GET|POST /problems` — catalog (M3/M6)
- `POST /sessions` — create session + mint LiveKit token (M2)
- `GET /sessions/{id}` — session status (M2)
- `POST /sessions/{id}/end` — graceful end (M2)
- `DELETE /sessions/{id}` — purge artifacts (M6)
- `GET /sessions/{id}/report` — report (M5)
- `POST /livekit/token` — token refresh (M1)

## Auth

JWTs minted by Supabase Auth (GoTrue) are verified locally using
`API_JWT_SECRET`, which must match `GOTRUE_JWT_SECRET` in the compose stack.
The `GOTRUE_JWT_AUD=authenticated` audience is enforced.
