#!/usr/bin/env bash
# dev.sh — boot the ArchMentor local stack.
#
# Starts infra/docker-compose.yml services, waits for them to be healthy,
# then prints next-step commands for the API, agent, and web processes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# ─── .env ────────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  echo "• .env not found; copying .env.example → .env"
  echo "  EDIT .env and replace placeholder secrets before running sessions."
  cp .env.example .env
fi

# ─── Preflight ───────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || {
  echo "✗ docker not found on PATH" >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "✗ 'docker compose' plugin not available" >&2
  exit 1
}

# ─── Up ─────────────────────────────────────────────────────────────────
COMPOSE="docker compose -f infra/docker-compose.yml --env-file .env"

echo "• docker compose up -d"
$COMPOSE up -d

# ─── Wait for health ─────────────────────────────────────────────────────
echo "• waiting for services to report healthy…"
deadline=$(( $(date +%s) + 90 ))
while :; do
  unhealthy=$($COMPOSE ps --format '{{.Service}} {{.Health}}' \
    | awk '$2 != "healthy" && $2 != "" {print $1}' || true)
  if [[ -z "$unhealthy" ]]; then
    break
  fi
  if (( $(date +%s) > deadline )); then
    echo "✗ services still unhealthy after 90s:" >&2
    echo "$unhealthy" >&2
    exit 1
  fi
  sleep 2
done

echo "✓ all services healthy"
$COMPOSE ps

# ─── Warm model caches ──────────────────────────────────────────────────
# Kokoro + NLTK punkt land in `.model-cache/` (gitignored). Skipped if
# the `audio` extra isn't installed — CI/Linux boxes don't need it.
if [[ -n "${SKIP_WARM_MODELS:-}" ]]; then
  echo "• skipping model warm-up (SKIP_WARM_MODELS set)"
else
  echo "• warming model caches (Kokoro + NLTK) — first run downloads ~300MB"
  uv run python scripts/warm_models.py || {
    echo "⚠ model warm-up failed; agent will still try lazy-load at first session." >&2
  }
fi

cat <<'NEXT'

──────────────────────────────────────────────────────────────────────
Next steps (each in its own terminal):

  • FastAPI:  uv run --package archmentor-api \
                uvicorn archmentor_api.main:app --reload --port 8000

  • Agent:    uv run --package archmentor-agent \
                python -m archmentor_agent.main dev

  • Web:      pnpm --filter @archmentor/web dev

Then open http://localhost:3000
──────────────────────────────────────────────────────────────────────
NEXT
