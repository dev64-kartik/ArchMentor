"""Test fakes shared between event-router and main-entrypoint tests.

Kept under `tests/` (not `archmentor_agent/`) so production never
imports them. Production callers depend on `BrainClient` /
`RedisSessionStore` / `SnapshotClient` directly.
"""

from .brain import FakeBrainClient
from .snapshots import FakeSnapshotClient
from .store import FakeSessionStore

__all__ = ["FakeBrainClient", "FakeSessionStore", "FakeSnapshotClient"]
