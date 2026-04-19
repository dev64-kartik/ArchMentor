"""Decision-point state serialization.

Every brain call writes a full snapshot (SessionState + event payload +
brain output + reasoning + tokens) to the `brain_snapshots` table.
Replayable via `scripts/replay.py --snapshot <id>`.

Implementation lands in M2.
"""
