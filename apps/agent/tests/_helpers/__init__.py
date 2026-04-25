"""Test fakes shared between event-router and main-entrypoint tests.

Kept under `tests/` (not `archmentor_agent/`) so production never
imports them. Production callers depend on `BrainClient` /
`RedisSessionStore` / `SnapshotClient` directly.

`apps/agent/conftest.py` injects `apps/agent/tests/` into ``sys.path``
so we can use absolute imports here instead of relative ones — see the
global CLAUDE.md rule "Absolute imports only — no relative (`..`) paths".
"""

from _helpers.brain import FakeBrainClient
from _helpers.canvas import FakeCanvasSnapshotClient
from _helpers.snapshots import FakeSnapshotClient
from _helpers.store import FakeSessionStore

__all__ = [
    "FakeBrainClient",
    "FakeCanvasSnapshotClient",
    "FakeSessionStore",
    "FakeSnapshotClient",
]
