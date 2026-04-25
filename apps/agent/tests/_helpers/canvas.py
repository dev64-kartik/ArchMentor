"""In-memory `CanvasSnapshotClient` substitute.

Records each `append` call so tests can assert on cadence + payload
shape. Mirrors `FakeSnapshotClient` deliberately — same pattern, same
ergonomics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class _RecordedCanvasSnapshot:
    session_id: UUID
    t_ms: int
    scene_json: dict[str, Any]


@dataclass
class FakeCanvasSnapshotClient:
    """Records every canvas snapshot append; never errors."""

    posts: list[_RecordedCanvasSnapshot] = field(default_factory=list)
    return_value: bool = True

    async def append(
        self,
        *,
        session_id: UUID,
        t_ms: int,
        scene_json: dict[str, Any],
    ) -> bool:
        self.posts.append(
            _RecordedCanvasSnapshot(
                session_id=session_id,
                t_ms=t_ms,
                scene_json=dict(scene_json),
            )
        )
        return self.return_value

    async def aclose(self) -> None:
        return None


__all__ = ["FakeCanvasSnapshotClient"]
