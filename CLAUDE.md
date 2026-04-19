# ArchMentor — Project Instructions

Project-specific overrides on top of `~/.claude/CLAUDE.md`.

## What this is

AI-powered live system design interview mentor. Candidate solves a problem by speaking and drawing on an embedded Excalidraw whiteboard; an AI interviewer observes continuously, interrupts at natural discourse boundaries, and generates a structured feedback report after the 45-minute session. See the plan for the specific stack (LiveKit Agents, Claude Opus tool-use, whisper.cpp, Kokoro, Excalidraw, FastAPI, Next.js).

**Primary plan:** `docs/plans/2026-04-17-001-feat-ai-system-design-mentor-plan.md` — treat as the decision artifact. Milestones live there.

## Monorepo layout

- `apps/web` — Next.js 15 frontend (pnpm, `@archmentor/web`)
- `apps/api` — FastAPI control plane (uv, `archmentor-api`)
- `apps/agent` — LiveKit Agent worker (uv, `archmentor-agent`)
- `packages/problems` — YAML problem definitions
- `packages/prompts` — shared prompt fragments and rubrics
- `infra/` — docker-compose stack (LiveKit, Postgres, GoTrue, Redis, MinIO, Langfuse)
- `scripts/` — dev/ops scripts (`dev.sh`, `seed_problems.py`, `replay.py`)
- `tests/` — eval harness + shared fixtures

## Workspace roots

- Python: `uv` workspace at repo root (`pyproject.toml`). Members: `apps/api`, `apps/agent`.
- JS: `pnpm` workspace at repo root (`pnpm-workspace.yaml`). Members: `apps/web`, `packages/*`.

## Commands per app

| App | Install | Run dev | Lint | Typecheck | Test |
|---|---|---|---|---|---|
| `apps/web` | `pnpm install` | `pnpm --filter @archmentor/web dev` | `pnpm --filter @archmentor/web lint` | `pnpm --filter @archmentor/web typecheck` | `pnpm --filter @archmentor/web test` |
| `apps/api` | `uv sync --all-packages` | `uv run --package archmentor-api uvicorn archmentor_api.main:app --reload` | `uv run ruff check apps/api` | `uv run ty check apps/api` | `uv run pytest apps/api` |
| `apps/agent` | `uv sync --all-packages` | `uv run --package archmentor-agent python -m archmentor_agent.main dev` | `uv run ruff check apps/agent` | `uv run ty check apps/agent` | `uv run pytest apps/agent` |

Python workspace is at repo root — `uv sync` alone only installs root dev deps. Use `--all-packages` to install both API and agent.

Migrations (from `apps/api/`):

```bash
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
```

Local stack: `./scripts/dev.sh` boots docker-compose services.

## First run

Order matters — schema and auth depend on Postgres being up.

```bash
cp .env.example .env                             # 1. fill in secrets; leave defaults for local dev
pnpm install && uv sync --all-packages           # 2. install workspace deps
./scripts/dev.sh                                 # 3. boot docker-compose (postgres, redis, minio, gotrue, langfuse, livekit)
(cd apps/api && uv run alembic upgrade head)     # 4. apply initial migration to archmentor DB
```

Then start the three app processes per the commands table. `API_JWT_SECRET` in `.env` must equal `GOTRUE_JWT_SECRET` — the API verifies GoTrue tokens using the shared secret.

## Project-specific rules

- **Tool-use, not JSON.** Brain output always flows through Anthropic tool-use. Never parse free-form text as structured output.
- **Decisions log is sacred.** The `DesignDecision` list in `SessionState` is never compressed. Summary compression runs on transcript, never on decisions.
- **Serialized event router.** Only one brain call in flight at a time. Concurrent events coalesce into a single call; don't parallelize.
- **Append-only event ledger from day one.** Every session event (utterance, canvas diff, brain decision + reasoning, phase transition) is written to `session_events` with `{session_id, t_ms, type, payload_json}`. This underpins replay, eval harness, and all analytics.
- **Brain snapshots.** Every brain call serializes full `SessionState` + event payload + brain output + reasoning to `brain_snapshots`. Replayable via `scripts/replay.py`.
- **No TTL on Redis session keys.** Explicit cleanup on session end. Prevents silent state eviction during pauses.
- **Transcript is untrusted input.** System prompt must reject embedded instructions.
- **Prompt caching on static prefix.** Problem + rubric + system prompt are cache-stable; rolling transcript is per-call.
- **Confidence-gated interruption.** Brain emits a confidence score; abstain below 0.6. Log the moment for prompt iteration.
- **Shared JWT secret.** `API_JWT_SECRET` (FastAPI verifier) must equal `GOTRUE_JWT_SECRET` (GoTrue signer). Drift silently turns every `/me` request into 401.

## Agent-native

Features are outcomes, not UI-first endpoints. Any action a candidate can take, an agent tool should also be able to drive (useful for eval harness and automated session replay).

## Dependencies

When adding dependencies, look up the current stable version — never assume from memory. Pin exact versions. Justify each new dependency.

## Current milestone

See `docs/plans/2026-04-17-001-feat-ai-system-design-mentor-plan.md` — milestones M0…M6.

## Gotchas

- **Claude Code sandbox + uv cache.** `~/.cache/uv` is not writable in this sandbox; the repo pins `cache-dir = ".uv-cache"` in root `pyproject.toml` to route uv's cache into the project. Running `uv` from a subdir (e.g., `cd apps/api && uv run alembic …`) creates a second `.uv-cache/` there — gitignored, harmless, but can be deleted.
- **Claude Code sandbox + Turbopack.** `next build` default (Turbopack) can't bind loopback ports under the sandbox. Use `next build --webpack` here; normal dev unaffected.
- **Agent subpackage `queue/`.** Intentionally shadows stdlib `queue`; ruff `A005` is ignored per-file in root `pyproject.toml`.
- **pytest cross-app collection.** `apps/api/tests` and `apps/agent/tests` both live under a `tests` package name. Running from repo root requires `--import-mode=importlib` (already set in root `pyproject.toml`).
- **GoTrue search_path.** `GOTRUE_DB_DATABASE_URL` must end with `?search_path=auth` — without it, the Go driver inherits the default `public` schema and runtime queries miss `auth.*` tables.
- **Version lookups over assumptions.** Before adding a dep or bumping a pin, fetch the current stable from the registry. The plan doc's versions are a snapshot; the repo pins are what's live.
