"""Decision-point state serialization.

Every brain call writes a full snapshot (SessionState + event payload +
brain output + reasoning + tokens) to the `brain_snapshots` table.
Replayable via `scripts/replay.py --snapshot <id>`.
"""

from archmentor_agent.snapshots.client import SnapshotClient, SnapshotClientConfig
from archmentor_agent.snapshots.serializer import build_snapshot

__all__ = ["SnapshotClient", "SnapshotClientConfig", "build_snapshot"]
