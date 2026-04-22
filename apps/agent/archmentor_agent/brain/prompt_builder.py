"""Assemble the Anthropic `messages.create(...)` call inputs.

Split out from `client.py` so:

- Unit tests can assert the exact system-block + user-message shape
  without monkey-patching the SDK.
- `scripts/replay.py` can reuse the identical builder when replaying a
  historical `brain_snapshots` row — no drift between the live path
  and the replay path.

The system block is the only `cache_control`-tagged element. It stays
identical across every call in a session (problem + rubric + policy
prompt are static), so Anthropic's ephemeral prompt cache can match it
byte-for-byte. The dynamic state lives on the `messages[0]` user turn
and is never cached.

Prompt cache threshold caveat — Anthropic silently ignores
`cache_control={"type":"ephemeral"}` when the cached block is below the
per-model minimum (~4096 tokens on Opus 4.x). Below that, no cache-write
premium is charged but no cache reads happen either. The brain client
emits `cache_creation_input_tokens` + `cache_read_input_tokens` on every
snapshot so we can verify whether caching actually fires. Do not rely
on the marker to "guarantee" caching — it is a request, not a promise.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from archmentor_agent.state import SessionState

# brain/prompt_builder.py → brain → archmentor_agent → apps/agent
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_SYSTEM_MD_PATH = _PROMPTS_DIR / "system.md"

# SessionState fields that live in the cached system block (problem +
# prompt-version identifier). Excluding them from the per-call user
# turn prevents duplicating the problem statement (which is already in
# the system block via `problem.statement_md`) and keeps the dynamic
# payload tight. `set[str]` (not `frozenset`) because pydantic's
# `model_dump(exclude=...)` signature requires the mutable variant.
_STATIC_STATE_FIELDS: set[str] = {"problem", "system_prompt_version"}


@lru_cache(maxsize=1)
def _load_system_md() -> str:
    """Read `prompts/system.md` once per process.

    `lru_cache` is safe here — the file is packaged with the agent
    wheel; it doesn't change at runtime. Tests that mutate the file
    must call `_load_system_md.cache_clear()`.
    """
    return _SYSTEM_MD_PATH.read_text(encoding="utf-8")


def build_system_block(state: SessionState) -> dict[str, Any]:
    """Build the single cache-tagged system block.

    Order matters: the policy prompt first (stable across all sessions),
    then the problem statement, then the rubric YAML. The three are
    concatenated as one text block because Anthropic's cache works at
    the block level — splitting into three would cache them separately
    (three breakpoints, only one free) and misses the point.
    """
    text = "\n\n".join(
        [
            _load_system_md().rstrip(),
            f"# Problem: {state.problem.title}",
            state.problem.statement_md.rstrip(),
            "# Rubric",
            state.problem.rubric_yaml.rstrip(),
        ]
    )
    return {
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }


def build_user_message(state: SessionState, event_payload: dict[str, Any]) -> dict[str, Any]:
    """Build the single user-role message for this brain call.

    The `state_json` excludes `problem` and `system_prompt_version`
    because those live in the cached system block. Everything else
    (transcript_window, decisions log, rubric_coverage, phase,
    active_argument, etc.) is per-call dynamic and must be in the
    uncached message.
    """
    state_json = state.model_dump(mode="json", exclude=_STATIC_STATE_FIELDS)
    # Two labeled JSON blobs rather than one merged object — the labels
    # make the brain's job of distinguishing "what the candidate just
    # did" (event) from "everything that happened before" (state)
    # explicit. A merged object loses that boundary.
    body = (
        "# Session state\n"
        "```json\n"
        f"{json.dumps(state_json, ensure_ascii=False, sort_keys=True)}\n"
        "```\n\n"
        "# Event\n"
        "```json\n"
        f"{json.dumps(event_payload, ensure_ascii=False, sort_keys=True)}\n"
        "```\n\n"
        "Emit one `interview_decision` tool call."
    )
    return {"role": "user", "content": body}


def build_call_kwargs(
    state: SessionState,
    event_payload: dict[str, Any],
    *,
    model: str,
    tool: Mapping[str, Any],
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """Bundle the full `messages.create(...)` kwargs.

    Returning a kwargs dict instead of positional args lets both the
    live client and `scripts/replay.py` pass the same structure to the
    SDK without redefining each field.

    `tool` is typed as `Mapping` rather than `dict` so callers can pass
    `INTERVIEW_DECISION_TOOL` (a TypedDict) directly — ty's overload
    resolution rejects `dict(typed_dict)` without an explicit cast,
    and a TypedDict is structurally a Mapping already.
    """
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [build_system_block(state)],
        "tools": [dict(tool)],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [build_user_message(state, event_payload)],
    }
