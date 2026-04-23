"""In-memory `SnapshotClient` substitute.

`append(...)` records the body and returns True. Tests assert against
`posts` to confirm one snapshot per dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class _RecordedSnapshot:
    session_id: UUID
    t_ms: int
    session_state_json: dict[str, Any]
    event_payload_json: dict[str, Any]
    brain_output_json: dict[str, Any]
    reasoning_text: str
    tokens_input: int
    tokens_output: int


@dataclass
class FakeSnapshotClient:
    """Records every snapshot append; never errors."""

    posts: list[_RecordedSnapshot] = field(default_factory=list)
    return_value: bool = True

    async def append(
        self,
        *,
        session_id: UUID,
        t_ms: int,
        session_state_json: dict[str, Any],
        event_payload_json: dict[str, Any],
        brain_output_json: dict[str, Any],
        reasoning_text: str = "",
        tokens_input: int = 0,
        tokens_output: int = 0,
    ) -> bool:
        self.posts.append(
            _RecordedSnapshot(
                session_id=session_id,
                t_ms=t_ms,
                session_state_json=session_state_json,
                event_payload_json=event_payload_json,
                brain_output_json=brain_output_json,
                reasoning_text=reasoning_text,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
            )
        )
        return self.return_value

    async def aclose(self) -> None:
        return None


__all__ = ["FakeSnapshotClient"]
