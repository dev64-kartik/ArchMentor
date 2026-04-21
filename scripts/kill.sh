#!/usr/bin/env bash
# kill.sh — stop the ArchMentor app processes (web + agent + api).
#
# `uv run`, `livekit-agents dev`, and `pnpm/next dev` each spawn child
# subprocesses that a plain Ctrl-C doesn't always propagate to. Rather
# than hunt for orphans by PID each time, match by command pattern.
#
# Leaves Colima + docker alone — `docker compose down` is the right
# tool for infra. Leaves port 7880/9999 alone — those are Colima's
# ssh-mux path into the docker VM, not our processes.
set -euo pipefail

# pkill exits 1 when nothing matches; that's fine for idempotent stop.
pkill_quiet() {
  pkill -f "$1" 2>/dev/null || true
}

pkill_quiet "archmentor_agent.main"
pkill_quiet "archmentor_api.main"
pkill_quiet "uvicorn archmentor_api"
pkill_quiet "next dev"
pkill_quiet "next-server"

# Second pass after a beat — catches uv/pnpm parents that respawn a
# child mid-signal. Takes <1s in the common case.
sleep 1
pkill_quiet "archmentor_agent.main"
pkill_quiet "archmentor_api.main"
pkill_quiet "next dev"
pkill_quiet "next-server"

# livekit-agents spawns its process pool via multiprocessing.spawn,
# whose children run with argv like `python -c "from
# multiprocessing.spawn import spawn_main; ..."`. The module-name
# pkill above never matches them; if the parent dies uncleanly they
# survive as orphans (PPID=1) and hold ~1 GB of whisper/kokoro memory
# each.
#
# Two fingerprints — we need both:
#   (a) Live workers with an open LiveKit websocket. Routes jobs to
#       them until deregistered.
#   (b) Dormant orphans whose websocket already closed (e.g. after a
#       LiveKit container restart). These don't match (a) but still
#       leak memory until reboot.
# We scope (b) to `.venv/bin/python3` inside this repo so it can't
# hit unrelated Python multiprocessing workers on the same machine.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
livekit_port="${LIVEKIT_PORT:-7880}"

worker_pids_by_port() {
  lsof -nP -iTCP:"$livekit_port" -sTCP:ESTABLISHED 2>/dev/null \
    | awk '/python/ {print $2}' | sort -u
}
worker_pids_by_argv() {
  pgrep -f "${repo_root}/.venv/bin/python3 -c from multiprocessing" 2>/dev/null || true
}

kill_workers() {
  local signal="$1" pids
  pids="$(
    {
      worker_pids_by_port
      worker_pids_by_argv
    } | sort -u
  )"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill "$signal" 2>/dev/null || true
  fi
}

kill_workers -TERM
sleep 1
kill_workers -KILL

# Sanity check: anything still bound to our dev ports?
if lsof -ti:3000,8000 >/dev/null 2>&1; then
  echo "⚠ something is still holding :3000 or :8000 — force-killing"
  lsof -ti:3000,8000 | xargs kill -9 2>/dev/null || true
fi

echo "✓ archmentor app processes stopped"
echo "  docker containers left running — use \`docker compose -f infra/docker-compose.yml down\` to stop them too"
