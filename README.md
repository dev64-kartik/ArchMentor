# ArchMentor

Practice system design with live AI feedback.

A candidate picks a problem, solves it by speaking and drawing on an
embedded Excalidraw whiteboard, while an AI interviewer (Claude Opus 4.6,
tool-use mode) observes continuously, interrupts at natural discourse
boundaries, and generates a structured feedback report after the 45-minute
session.

## Status

M0 — **foundation scaffolding**. See
`docs/plans/2026-04-17-001-feat-ai-system-design-mentor-plan.md` for the
full roadmap (M0…M6).

## Layout

```
apps/
  web/      Next.js 15 frontend (pnpm, @archmentor/web)
  api/      FastAPI control plane (uv, archmentor-api)
  agent/    LiveKit Agent worker (uv, archmentor-agent)
packages/
  problems/ YAML problem definitions
  prompts/  Shared prompt fragments + rubrics
infra/      docker-compose stack (LiveKit, Postgres, GoTrue, Redis, MinIO, Langfuse)
scripts/    dev.sh, seed_problems.py, replay.py
tests/      Eval harness + shared fixtures
```

## Quick start

Requires: Docker, Node 22+, pnpm 10+, uv.

```bash
cp .env.example .env            # fill in secrets
pnpm install                    # install JS workspace deps
uv sync                         # install Python workspace deps
./scripts/dev.sh                # boot docker-compose stack
```

Then, in separate terminals:

```bash
uv run --package archmentor-api uvicorn archmentor_api.main:app --reload --port 8000
uv run --package archmentor-agent python -m archmentor_agent.main dev
pnpm --filter @archmentor/web dev
```

Open <http://localhost:3000>.

## Contributing

See `CLAUDE.md` for project conventions. The plan in `docs/plans/` is the
decision artifact — treat milestones there as the source of truth.

## License

Apache 2.0 — see `LICENSE`.
