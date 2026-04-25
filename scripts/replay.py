"""Replay a `brain_snapshots` row through the current prompt/tools.

Usage:
    # Preview the reconstructed request (no Anthropic call):
    uv run python scripts/replay.py --snapshot <uuid>

    # Actually invoke the current brain client and diff the result:
    uv run python scripts/replay.py --snapshot <uuid> --run

    # Drive the M3 lifecycle end-to-end against a running stack:
    uv run python scripts/replay.py --lifecycle \\
        --email you@example.com --password ...

Exit codes:
    0 — stored and fresh decisions agree on decision + priority + confidence
        (or — for --lifecycle — every step passed)
    1 — the three match-keys disagree (a prompt / schema drift signal)
        or any --lifecycle step failed
    2 — snapshot not found (or other operator error)
    3 — usage error (bad CLI args, missing env, etc.)

`--dry-run` is the default so a misclick in `watch uv run …` doesn't
burn Anthropic tokens. Pass `--run` to actually invoke the brain.

A `--session <uuid>` multi-snapshot replay mode is reserved but gated
to a loud `SystemExit` — unbounded replays land in a later milestone.
"""
# ruff: noqa: T201  # dev-only CLI — print output is the UX here

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

# Load repo-root `.env` before importing `archmentor_*`. Both the API
# and agent Settings classes run placeholder-rejection at import time;
# without this, running the script from a shell that hasn't already
# exported `.env` keys fails with a confusing "missing placeholder"
# error. Shell-wins (no `override=True`) so an explicit export still
# beats the file — matches the non-dev posture in `main.py`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

# Registers SQLModel metadata + pulls the API's Settings singleton.
import archmentor_api.models  # noqa: F401, E402 — register tables on metadata
from archmentor_agent.brain.client import BrainClient, get_brain_client  # noqa: E402
from archmentor_agent.brain.decision import BrainDecision  # noqa: E402
from archmentor_agent.brain.pricing import BRAIN_MODEL  # noqa: E402
from archmentor_agent.brain.prompt_builder import build_call_kwargs  # noqa: E402
from archmentor_agent.brain.tools import INTERVIEW_DECISION_TOOL  # noqa: E402
from archmentor_agent.state.session_state import SessionState  # noqa: E402
from archmentor_api.db import engine  # noqa: E402
from archmentor_api.models.brain_snapshot import BrainSnapshot  # noqa: E402
from sqlmodel import Session  # noqa: E402

# Mirrors `archmentor_agent.brain.client._MAX_TOKENS`. Keeping it
# duplicated (not imported) keeps the replay path deterministic even if
# someone tweaks the live client's ceiling — a replay against a 2048-
# token ceiling must still pass 1024 to the model that wrote the row.
_REPLAY_MAX_TOKENS = 1024

# Exit codes are load-bearing for `scripts/smoke_brain.py` and future CI.
EXIT_MATCH = 0
EXIT_DIVERGED = 1
EXIT_NOT_FOUND = 2
# Distinct from NOT_FOUND so an automated caller can tell "I invoked
# the script wrong" (missing/invalid args) from "the snapshot UUID
# resolved but no matching row exists in Postgres."
EXIT_USAGE = 3


BrainClientFactory = Callable[[], BrainClient]


@dataclass(frozen=True)
class ReplayDiff:
    """Columns extracted from stored vs fresh decisions for display."""

    decision: tuple[str | None, str | None]
    priority: tuple[str | None, str | None]
    confidence: tuple[float | None, float | None]
    utterance: tuple[str | None, str | None]
    reasoning: tuple[str | None, str | None]

    @property
    def match_keys_agree(self) -> bool:
        """Stored and fresh agree on decision + priority + confidence.

        Utterance and reasoning text drift with Opus sampling even at
        temperature 0 (tokenizer jitter across SDK versions), so they
        don't gate exit codes — only the structured match keys do.
        """
        return (
            self.decision[0] == self.decision[1]
            and self.priority[0] == self.priority[1]
            and self.confidence[0] == self.confidence[1]
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay a brain_snapshots row through the current brain client.",
    )
    parser.add_argument(
        "--snapshot",
        metavar="UUID",
        help="Snapshot id to replay.",
    )
    parser.add_argument(
        "--session",
        metavar="UUID",
        help="(RESERVED — not yet implemented) Session id for multi-snapshot replay.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Actually invoke Anthropic. Without this flag, the script "
        "prints the reconstructed request without issuing any API call.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required on multi-snapshot replays (`--session`) to confirm the token spend.",
    )
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="Drive the full M3 lifecycle (POST /sessions → /events → "
        "/canvas-snapshots → /end → DELETE) and assert cascade. Requires "
        "--email + --password and a running stack.",
    )
    parser.add_argument(
        "--email",
        help="GoTrue email for --lifecycle sign-in.",
    )
    parser.add_argument(
        "--password",
        help="GoTrue password for --lifecycle sign-in.",
    )
    return parser


def _reject_session_mode(args: argparse.Namespace) -> None:
    """Multi-snapshot replay is reserved — bail loudly if attempted."""
    if args.session is not None:
        print(
            "`--session` replay is reserved for a later milestone. "
            "Use `--snapshot <uuid>` to replay a single row.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_USAGE)


def _load_snapshot(session_id: str) -> BrainSnapshot | None:
    try:
        snapshot_uuid = UUID(session_id)
    except ValueError as exc:
        raise SystemExit(f"`--snapshot` must be a UUID: {exc}") from exc
    with Session(engine) as db:
        return db.get(BrainSnapshot, snapshot_uuid)


def _state_from_snapshot(row: BrainSnapshot) -> SessionState:
    """Rehydrate the stored SessionState for the brain call."""
    return SessionState.model_validate(row.session_state_json)


def _stored_decision_summary(row: BrainSnapshot) -> dict[str, Any]:
    """Pull the stored decision fields out of `brain_output_json`.

    The router writes snapshots via `snapshots/serializer.build_snapshot`
    with `brain_output` = `_decision_payload(decision)`; the keys we
    read here mirror that shape so drifts in serialization surface
    at the deserialization boundary, not in a silent KeyError.
    """
    data = dict(row.brain_output_json)
    return {
        "decision": data.get("decision"),
        "priority": data.get("priority"),
        "confidence": data.get("confidence"),
        "utterance": data.get("utterance"),
        "reasoning": data.get("reasoning"),
    }


def _fresh_decision_summary(decision: BrainDecision) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "priority": decision.priority,
        "confidence": decision.confidence,
        "utterance": decision.utterance,
        "reasoning": decision.reasoning,
    }


def _build_diff(stored: dict[str, Any], fresh: dict[str, Any]) -> ReplayDiff:
    return ReplayDiff(
        decision=(stored["decision"], fresh["decision"]),
        priority=(stored["priority"], fresh["priority"]),
        confidence=(stored["confidence"], fresh["confidence"]),
        utterance=(stored["utterance"], fresh["utterance"]),
        reasoning=(stored["reasoning"], fresh["reasoning"]),
    )


def _print_dry_run(state: SessionState, event_payload: dict[str, Any]) -> None:
    """Show the reconstructed system + user message blocks."""
    kwargs = build_call_kwargs(
        state,
        event_payload,
        model=BRAIN_MODEL,
        tool=INTERVIEW_DECISION_TOOL,
        max_tokens=_REPLAY_MAX_TOKENS,
    )
    print("=== replay: dry-run — no Anthropic call ===")
    print(f"model: {kwargs['model']}")
    system_block = kwargs["system"][0]
    print(f"system block length: {len(system_block['text'])} chars")
    print("---- system block (truncated) ----")
    print(system_block["text"][:800] + ("…" if len(system_block["text"]) > 800 else ""))
    user = kwargs["messages"][0]
    print("---- user message (truncated) ----")
    content = user["content"]
    print(content[:1200] + ("…" if len(content) > 1200 else ""))
    print("Pass --run to actually invoke the brain.")


def _print_diff(stored: dict[str, Any], fresh: dict[str, Any], diff: ReplayDiff) -> None:
    """Three-column summary for operator review."""

    def _short(text: str | None, n: int) -> str:
        if text is None:
            return "-"
        return text[:n] + ("…" if len(text) > n else "")

    def _row(label: str, stored_val: object, fresh_val: object, changed: bool) -> str:
        marker = "≠" if changed else "="
        return f"  {label:<12} {marker}  stored={stored_val!r:<40} fresh={fresh_val!r}"

    print("=== replay: diff ===")
    print(
        _row(
            "decision",
            diff.decision[0],
            diff.decision[1],
            diff.decision[0] != diff.decision[1],
        )
    )
    print(
        _row(
            "priority",
            diff.priority[0],
            diff.priority[1],
            diff.priority[0] != diff.priority[1],
        )
    )
    print(
        _row(
            "confidence",
            diff.confidence[0],
            diff.confidence[1],
            diff.confidence[0] != diff.confidence[1],
        )
    )
    print(
        _row(
            "utterance",
            _short(stored["utterance"], 50),
            _short(fresh["utterance"], 50),
            stored["utterance"] != fresh["utterance"],
        )
    )
    print(
        _row(
            "reasoning",
            _short(stored["reasoning"], 50),
            _short(fresh["reasoning"], 50),
            stored["reasoning"] != fresh["reasoning"],
        )
    )
    print(
        f"match_keys_agree: {diff.match_keys_agree} "
        f"(decision/priority/confidence — exit=0 if True, 1 if False)"
    )


def _refuse_placeholder_key() -> None:
    """Fail closed before the brain client asks Anthropic anything.

    The brain client's own `Settings` validator rejects the
    `.env.example` placeholder, but in a `--run` replay we want an
    extra-obvious exit — operator staged a run, we want to be certain
    the real key is on the shell.
    """
    raw = os.environ.get("ARCHMENTOR_ANTHROPIC_API_KEY", "")
    if not raw or "replace_with_" in raw:
        raise SystemExit(
            "ARCHMENTOR_ANTHROPIC_API_KEY is missing or a placeholder. "
            "Export a real key before running replay with --run."
        )


async def _run_brain_once(
    state: SessionState,
    event_payload: dict[str, Any],
    t_ms: int,
    *,
    brain: BrainClient,
) -> BrainDecision:
    """One-shot brain call matching the live `decide(...)` contract."""
    return await brain.decide(state=state, event=event_payload, t_ms=t_ms)


def run_replay(
    snapshot_id: str,
    *,
    run: bool = False,
    brain_factory: BrainClientFactory = get_brain_client,
) -> int:
    """Entry point both the CLI and tests call.

    Returns an exit code (see EXIT_* constants). Tests inject
    `brain_factory` to return a `FakeBrainClient`; production hands the
    real singleton.
    """
    row = _load_snapshot(snapshot_id)
    if row is None:
        print(f"snapshot not found: {snapshot_id}", file=sys.stderr)
        return EXIT_NOT_FOUND

    state = _state_from_snapshot(row)
    event_payload = dict(row.event_payload_json)

    if not run:
        _print_dry_run(state, event_payload)
        return EXIT_MATCH

    _refuse_placeholder_key()

    import asyncio

    brain = brain_factory()
    decision = asyncio.run(
        _run_brain_once(state, event_payload, row.t_ms, brain=brain),
    )
    stored = _stored_decision_summary(row)
    fresh = _fresh_decision_summary(decision)
    diff = _build_diff(stored, fresh)
    _print_diff(stored, fresh, diff)
    return EXIT_MATCH if diff.match_keys_agree else EXIT_DIVERGED


# ──────────────── --lifecycle: M3 full-stack smoke ────────────────────


_LIFECYCLE_CASCADE_TABLES = (
    "session_events",
    "brain_snapshots",
    "canvas_snapshots",
    "interruptions",
    "reports",
)


def _gotrue_url() -> str:
    """Resolve the GoTrue base URL from the same env var the web uses."""
    url = os.environ.get("NEXT_PUBLIC_GOTRUE_URL") or os.environ.get("GOTRUE_API_EXTERNAL_URL")
    if not url:
        raise SystemExit(
            "NEXT_PUBLIC_GOTRUE_URL (or GOTRUE_API_EXTERNAL_URL) is not set; "
            "cannot sign in for --lifecycle."
        )
    return url.rstrip("/")


def _api_url() -> str:
    """Reuse the agent's api_url field so dev + CI agree on one knob."""
    from archmentor_agent.config import get_settings as _agent_settings

    return _agent_settings().api_url.rstrip("/")


def _agent_token() -> str:
    """Shared-secret for X-Agent-Token. Same source as the live agent."""
    from archmentor_agent.config import get_settings as _agent_settings

    return _agent_settings().agent_ingest_token.get_secret_value()


def _print_step(name: str, ok: bool, detail: str = "") -> None:
    marker = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{marker}] {name}{suffix}")


async def _run_lifecycle(email: str, password: str) -> int:
    """End-to-end smoke: sign-in → /sessions → /events → /canvas-snapshots
    → /end → DELETE → cascade audit. Prints per-step pass/fail and
    returns EXIT_MATCH on full success, EXIT_DIVERGED on any failure.

    Built directly on httpx + sqlmodel rather than spinning up FastAPI's
    test client because the goal is to exercise the *running* stack
    (uvicorn, GoTrue, Postgres) the way a candidate-driven session does.
    """
    import httpx
    from sqlalchemy import text

    api_url = _api_url()
    gotrue_url = _gotrue_url()
    agent_token = _agent_token()

    print("=== replay: --lifecycle smoke ===")
    print(f"  api: {api_url}")
    print(f"  gotrue: {gotrue_url}")

    failures: list[str] = []

    async with httpx.AsyncClient(timeout=15.0) as http:
        # Step 1: sign in via GoTrue's password grant.
        token_resp = await http.post(
            f"{gotrue_url}/token?grant_type=password",
            json={"email": email, "password": password},
        )
        if token_resp.status_code != 200:
            detail = f"HTTP {token_resp.status_code}: {token_resp.text}"
            _print_step("gotrue sign-in", False, detail)
            return EXIT_DIVERGED
        access_token = token_resp.json().get("access_token")
        if not access_token:
            _print_step("gotrue sign-in", False, "no access_token in response")
            return EXIT_DIVERGED
        _print_step("gotrue sign-in", True)
        user_headers = {"Authorization": f"Bearer {access_token}"}
        agent_headers = {"X-Agent-Token": agent_token}

        # Step 2: pick a problem.
        problems_resp = await http.get(f"{api_url}/problems", headers=user_headers)
        if problems_resp.status_code != 200 or not problems_resp.json():
            _print_step(
                "GET /problems",
                False,
                f"HTTP {problems_resp.status_code}: {problems_resp.text}",
            )
            return EXIT_DIVERGED
        problem_slug = problems_resp.json()[0]["slug"]
        _print_step("GET /problems", True, f"using slug={problem_slug!r}")

        # Step 3: create the session.
        create_resp = await http.post(
            f"{api_url}/sessions",
            headers=user_headers,
            json={"problem_slug": problem_slug},
        )
        if create_resp.status_code != 201:
            _print_step(
                "POST /sessions",
                False,
                f"HTTP {create_resp.status_code}: {create_resp.text}",
            )
            return EXIT_DIVERGED
        session_id = create_resp.json()["session_id"]
        _print_step("POST /sessions", True, f"id={session_id}")

        # Step 4: agent appends a turn_end event.
        event_resp = await http.post(
            f"{api_url}/sessions/{session_id}/events",
            headers=agent_headers,
            json={
                "t_ms": 1_000,
                "type": "turn_end",
                "payload_json": {"transcripts": ["hello mentor"]},
            },
        )
        if event_resp.status_code != 201:
            failures.append("POST /events")
            detail = f"HTTP {event_resp.status_code}: {event_resp.text}"
            _print_step("POST /events (agent)", False, detail)
        else:
            _print_step("POST /events (agent)", True)

        # Step 5: agent appends a canvas snapshot.
        canvas_resp = await http.post(
            f"{api_url}/sessions/{session_id}/canvas-snapshots",
            headers=agent_headers,
            json={
                "t_ms": 2_000,
                "scene_json": {
                    "elements": [
                        {"id": "a", "type": "rectangle", "x": 0, "y": 0},
                    ],
                    "appState": {},
                },
            },
        )
        if canvas_resp.status_code != 201:
            failures.append("POST /canvas-snapshots")
            _print_step(
                "POST /canvas-snapshots",
                False,
                f"HTTP {canvas_resp.status_code}: {canvas_resp.text}",
            )
        else:
            _print_step("POST /canvas-snapshots", True)

        # Step 6: end the session.
        end_resp = await http.post(
            f"{api_url}/sessions/{session_id}/end",
            headers=user_headers,
        )
        if end_resp.status_code != 200 or end_resp.json().get("status") != "ended":
            failures.append("POST /end")
            _print_step(
                "POST /end",
                False,
                f"HTTP {end_resp.status_code}: {end_resp.text}",
            )
        else:
            _print_step("POST /end", True)

        # Step 7: delete and assert cascade.
        delete_resp = await http.delete(
            f"{api_url}/sessions/{session_id}",
            headers=user_headers,
        )
        if delete_resp.status_code != 204:
            failures.append("DELETE /sessions/{id}")
            _print_step(
                "DELETE /sessions/{id}",
                False,
                f"HTTP {delete_resp.status_code}: {delete_resp.text}",
            )
        else:
            _print_step("DELETE /sessions/{id}", True)

    # Cascade audit — any orphan child rows mean a missing ON DELETE
    # CASCADE. We talk straight to Postgres because the API has no
    # endpoint that exposes raw row counts for a deleted session.
    with engine.connect() as conn:
        for table in _LIFECYCLE_CASCADE_TABLES:
            # Table name is interpolated from the static
            # `_LIFECYCLE_CASCADE_TABLES` allowlist above; not user
            # input. The bound `:sid` is parametrized.
            query = text(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = :sid"  # noqa: S608
            )
            try:
                row_count = int(conn.execute(query, {"sid": session_id}).scalar_one())
            except Exception as exc:
                _print_step(f"cascade {table}", False, f"query failed: {exc}")
                failures.append(f"cascade {table}")
                continue
            if row_count == 0:
                _print_step(f"cascade {table}", True, "0 rows")
            else:
                _print_step(f"cascade {table}", False, f"{row_count} orphan rows")
                failures.append(f"cascade {table}")

    if failures:
        print(f"\n{len(failures)} failure(s): {', '.join(failures)}")
        return EXIT_DIVERGED
    print("\nlifecycle smoke: all steps passed.")
    return EXIT_MATCH


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.lifecycle:
        if not args.email or not args.password:
            print(
                "--lifecycle requires --email and --password.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        import asyncio

        return asyncio.run(_run_lifecycle(args.email, args.password))

    _reject_session_mode(args)
    if args.snapshot is None:
        # Distinct from EXIT_NOT_FOUND: an automated caller that sees
        # exit 3 knows it invoked the script wrong, not that the UUID
        # resolved but is absent from Postgres.
        print(
            "--snapshot <uuid> is required (--session is reserved, "
            "--lifecycle takes --email/--password).",
            file=sys.stderr,
        )
        return EXIT_USAGE
    # Redundant but explicit: only enforce --yes once `--session` is
    # actually wired. Preserved here so the argparse flag isn't dead.
    _ = args.yes
    return run_replay(args.snapshot, run=bool(args.run))


if __name__ == "__main__":
    sys.exit(main())
