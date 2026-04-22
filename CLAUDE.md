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

### Run every check (mirrors CI)

```bash
uv run ruff check . && uv run ruff format --check .
uv run ty check apps/api apps/agent
uv run pytest -q
pnpm -r lint && pnpm -r typecheck && pnpm -r test
```

CI (`.github/workflows/ci.yml`) runs the same set on push/PR — green here
means green there.

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
- **Shared JWT secret + issuer.** `API_JWT_SECRET` (FastAPI verifier) must equal `GOTRUE_JWT_SECRET` (GoTrue signer); drift silently turns every `/me` request into 401. Both are required — no placeholder default. `API_JWT_ISSUER` must match `GOTRUE_API_EXTERNAL_URL` so PyJWT enforces the `iss` claim.
- **`/livekit/token` is session-scoped.** Looks up `sessions.livekit_room`; 404 if absent, 403 if not the caller's session, 409 if not `ACTIVE`. Never mint tokens for arbitrary room strings.
- **`/gotrue/*` proxy is an explicit allowlist.** `apps/web/next.config.ts` enumerates GoTrue endpoints; a catch-all `/gotrue/:path*` would expose `/gotrue/admin/*` at same-origin — don't reintroduce.
- **Session-event ingest caps `payload_json` at 16 KiB** and rejects non-`ACTIVE` sessions with 409. Protects the append-only ledger and the M2 brain's rolling transcript.
- **Ledger writes are fire-and-forget.** `MentorAgent._log` schedules `ledger.append` on `_ledger_tasks`; the entrypoint drains the set before `ledger.aclose()`. Never `await` the ledger from a TTS-blocking path.
- **Agent-auth distinguishes 401 from 403.** Missing `X-Agent-Token` → 401; wrong token → 403. The ledger client treats all 4xx as permanent, so this separation lets a misconfigured token fail fast.

## Agent-native

Features are outcomes, not UI-first endpoints. Any action a candidate can take, an agent tool should also be able to drive (useful for eval harness and automated session replay).

## Dependencies

When adding dependencies, look up the current stable version — never assume from memory. Pin exact versions. Justify each new dependency.

## Current milestone

M0 (foundation) landed 2026-04-19. M1 (voice loop skeleton) ✅ done 2026-04-21. M2 (brain MVP + session persistence) ✅ landed 2026-04-22 on `feat/m2-brain-mvp` — Opus tool-use brain, serialized event router with coalescer, utterance queue + speech-check gate, Redis `SessionState` with no-TTL + CAS, `brain_snapshots` ingest route + replay CLI, Hinglish-friendly STT. See `docs/plans/2026-04-22-001-feat-m2-brain-mvp-plan.md` for the execution checkpoint and deferred work. **Next:** M3 — `POST /sessions`, Excalidraw canvas + canvas_change event priority, streaming LLM→TTS preamble.

### M1 audio extras + manual mic test

The agent ships Metal/MPS-only audio deps (`pywhispercpp`, `streaming-tts`) behind the `audio` extra so CI (Linux) can install the agent without native wheels:

```bash
uv sync --all-packages --extra audio   # macOS only
```

Use `/session/dev-test` (fixed room `session-dev-test`) to smoke-test the browser → LiveKit → agent path — M2 still runs through this route; `POST /sessions` lands in M3. See `docs/plans/2026-04-17-001-feat-ai-system-design-mentor-plan.md` for the full M0…M6 breakdown and `docs/plans/2026-04-22-001-feat-m2-brain-mvp-plan.md` for the M2 execution checkpoint.

After running `scripts/dev.sh` and `alembic upgrade head`, seed the dev problem + session:

```bash
uv run python scripts/seed_dev_session.py --email you@example.com
```

This writes the URL-shortener problem + rubric from `apps/agent/archmentor_agent/brain/bootstrap.py` to Postgres so the agent's in-memory `ProblemCard` matches the stored row byte-for-byte.

## Gotchas

- **Claude Code sandbox + uv cache.** `~/.cache/uv` is not writable in this sandbox; the repo pins `cache-dir = ".uv-cache"` in root `pyproject.toml` to route uv's cache into the project. Running `uv` from a subdir (e.g., `cd apps/api && uv run alembic …`) creates a second `.uv-cache/` there — gitignored, harmless, but can be deleted.
- **Claude Code sandbox + Turbopack.** `next build` default (Turbopack) can't bind loopback ports under the sandbox. Use `next build --webpack` here; normal dev unaffected.
- **Agent subpackage `queue/`.** Intentionally shadows stdlib `queue`; ruff `A005` is ignored per-file in root `pyproject.toml`.
- **pytest cross-app collection.** `apps/api/tests` and `apps/agent/tests` both live under a `tests` package name. Running from repo root requires `--import-mode=importlib` (already set in root `pyproject.toml`).
- **GoTrue search_path.** `GOTRUE_DB_DATABASE_URL` must end with `?search_path=auth` — without it, the Go driver inherits the default `public` schema and runtime queries miss `auth.*` tables.
- **JSONB degrades to JSON on SQLite.** `models/_base.py::jsonb_column` uses `JSONB().with_variant(JSON(), "sqlite")` so integration tests can spin up an in-memory SQLite engine. New models must use this helper — plain `JSONB` breaks the test harness.
- **Version lookups over assumptions.** Before adding a dep or bumping a pin, fetch the current stable from the registry. The plan doc's versions are a snapshot; the repo pins are what's live.
- **Audio extras are Apple Silicon only.** `pywhispercpp` (whisper.cpp Metal) and `streaming-tts` (Kokoro on MPS) live under the agent's `audio` extra. They are lazy-imported from `audio/stt.py` and `tts/kokoro.py` so CI on Linux can install the agent without them. Never move them into the required `dependencies` list.
- **Agent ingest secret, not user JWT.** The agent worker is a backend peer; it appends events via `X-Agent-Token` shared secret (`API_AGENT_INGEST_TOKEN` == `ARCHMENTOR_AGENT_INGEST_TOKEN`), not a Supabase JWT. Verified with `hmac.compare_digest` in `deps.require_agent`.
- **`load_dotenv(override=True)` is dev-only.** Agent `main.py` gates override on `ARCHMENTOR_ENV=dev`; any other value runs shell-wins so orchestrator-injected secrets aren't silently overwritten by a stale `.env`.
- **`.env.example` placeholders are rejected at startup.** `Settings` refuses any secret containing the `replace_with_` marker. Copying `.env.example` without editing fails loudly.
- **`WhisperCppSTT._resample_to_whisper_rate` raises on empty output.** Never fall back to the original wrong-rate buffer — whisper turns it into fabricated transcripts.
- **Model singletons use `threading.Lock`.** `audio/stt.py::_load_model` and `tts/kokoro.py::_load_engine` double-check-lock because they run on the default thread-pool executor. New singletons must follow the same pattern.
- **`scripts/kill.sh` for full teardown.** `pkill -f archmentor_agent.main` misses the `multiprocessing.spawn` workers livekit-agents dispatches; `kill.sh` sweeps orphans by venv path + websocket fingerprint.
- **Redis session keys have NO TTL by design.** `MentorAgent.shutdown()` calls `store.delete(session_id)` explicitly; a crashed worker leaves an orphan `session:<id>:state` key until M6's stale-session reaper. Manual cleanup: `redis-cli KEYS 'session:*:state' | xargs redis-cli DEL`.
- **`queue.dropped_stale` at push time = slow brain, not queue bug.** `t_ms` is assigned at dispatch entry (router invariant I3), before the Opus await. When a brain call takes ≥10s (7–15s per call observed on Opus 4.7 via Unbound), the utterance's `generated_at_ms` is already past TTL when `queue.push` runs, and the next `pop_if_fresh` drops it on first inspection. Signature in logs: `age_ms ≈ ttl_ms + brain_latency`. Bumping `PendingUtterance.ttl_ms` is the M2 knob; M4's streaming path is the real fix.
- **Dev ProblemCard is in two places deliberately.** `apps/agent/archmentor_agent/brain/bootstrap.py` defines the URL-shortener problem in-process (agent hands it to the brain at `on_enter`), and `scripts/seed_dev_session.py` writes the same strings to Postgres. Both read the same module constants — any edit must land in `bootstrap.py` and be followed by a re-seed. M3's `POST /sessions` replaces the in-process path with an API fetch.
- **`scripts/replay.py --snapshot` requires BOTH API and agent env vars.** The script imports `archmentor_api.db.engine` (which reads `API_*`) and `archmentor_agent.brain.client` (which reads `ARCHMENTOR_*`). Tests under `apps/agent/tests/` seed both sets at module import.
- **Brain client runs against Anthropic-compatible gateways, not just direct Anthropic.** `ARCHMENTOR_ANTHROPIC_BASE_URL` routes through a proxy (e.g. `https://api.getunbound.ai`); the Anthropic SDK auto-appends `/v1/messages`. Unbound (and LiteLLM-style gateways generally) use provider-prefixed model ids — default `ARCHMENTOR_BRAIN_MODEL` is `anthropic/claude-opus-4-7` for that reason. For direct Anthropic, set `ARCHMENTOR_BRAIN_MODEL=claude-opus-4-7`. Both ids are registered in `brain/pricing.py::BRAIN_RATES`; adding a new model id requires a new row there or `estimate_cost_usd` hard-raises `KeyError`.
