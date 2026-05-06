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
from _helpers.streaming import FakeAsyncMessageStream, FakeStreamEvent, utterance_deltas

__all__ = [
    "FakeAsyncMessageStream",
    "FakeBrainClient",
    "FakeCanvasSnapshotClient",
    "FakeSessionStore",
    "FakeSnapshotClient",
    "FakeStreamEvent",
    "utterance_deltas",
]
