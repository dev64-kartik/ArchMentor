"""Replay a `brain_snapshots` row through the current prompt/tools.

Usage:
    # Preview the reconstructed request (no Anthropic call):
    uv run python scripts/replay.py --snapshot <uuid>

    # Actually invoke the current brain client and diff the result:
    uv run python scripts/replay.py --snapshot <uuid> --run

Exit codes:
    0 — stored and fresh decisions agree on decision + priority + confidence
    1 — the three match-keys disagree (a prompt / schema drift signal)
    2 — snapshot not found (or other operator error)

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
from typing import Any
from uuid import UUID

# Registers SQLModel metadata + pulls the API's Settings singleton.
import archmentor_api.models  # noqa: F401 — register tables on metadata
from archmentor_agent.brain.client import BrainClient, get_brain_client
from archmentor_agent.brain.decision import BrainDecision
from archmentor_agent.brain.pricing import BRAIN_MODEL
from archmentor_agent.brain.prompt_builder import build_call_kwargs
from archmentor_agent.brain.tools import INTERVIEW_DECISION_TOOL
from archmentor_agent.state.session_state import SessionState
from archmentor_api.db import engine
from archmentor_api.models.brain_snapshot import BrainSnapshot
from sqlmodel import Session

# Mirrors `archmentor_agent.brain.client._MAX_TOKENS`. Keeping it
# duplicated (not imported) keeps the replay path deterministic even if
# someone tweaks the live client's ceiling — a replay against a 2048-
# token ceiling must still pass 1024 to the model that wrote the row.
_REPLAY_MAX_TOKENS = 1024

# Exit codes are load-bearing for `scripts/smoke_brain.py` and future CI.
EXIT_MATCH = 0
EXIT_DIVERGED = 1
EXIT_NOT_FOUND = 2


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
        help="Session id for multi-snapshot replay (deferred to a later milestone).",
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
    return parser


def _reject_session_mode(args: argparse.Namespace) -> None:
    """Multi-snapshot replay is reserved — bail loudly if attempted."""
    if args.session is not None:
        raise SystemExit(
            "`--session` replay is reserved for a later milestone. "
            "Use `--snapshot <uuid>` to replay a single row."
        )


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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _reject_session_mode(args)
    if args.snapshot is None:
        parser.error("--snapshot <uuid> is required (--session is reserved).")
    # Redundant but explicit: only enforce --yes once `--session` is
    # actually wired. Preserved here so the argparse flag isn't dead.
    _ = args.yes
    return run_replay(args.snapshot, run=bool(args.run))


if __name__ == "__main__":
    sys.exit(main())
