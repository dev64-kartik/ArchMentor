"""Replay a `brain_snapshots` row through the current prompt/tools.

Usage:
    uv run scripts/replay.py --snapshot <id>
    uv run scripts/replay.py --session <id> --at <t_ms>

Implementation lands in M2. Will load the snapshot, re-run the brain with
the current system prompt + tool schema, and diff the output against the
original (ghost diff). Used by the eval harness in M6 and during prompt
iteration.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit("replay.py is not implemented yet — lands in M2.")


if __name__ == "__main__":
    main()
