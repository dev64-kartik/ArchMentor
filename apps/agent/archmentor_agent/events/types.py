"""Event taxonomy for the brain dispatcher.

`RouterEvent` is the only shape the router consumes. M2 wires
`turn_end`, `long_silence`, and `phase_timer`; `canvas_change` is
declared so the M3 implementation can flip from the
`NotImplementedError` guard without renaming. `session_start`,
`wrapup_timer`, and `session_end` are placeholders for later milestones.

`payload` is intentionally typed as `dict[str, Any]`. Each event type
puts a different shape inside (transcript text, silence duration,
phase id, etc.) and constraining it would force a Union the router
doesn't actually inspect — the brain reads `payload` opaquely as part
of the `messages[0]` user turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    TURN_END = "turn_end"
    LONG_SILENCE = "long_silence"
    CANVAS_CHANGE = "canvas_change"
    PHASE_TIMER = "phase_timer"
    SESSION_START = "session_start"
    WRAPUP_TIMER = "wrapup_timer"
    SESSION_END = "session_end"


@dataclass(frozen=True, slots=True)
class RouterEvent:
    """One event handed to `EventRouter.handle(...)`.

    `t_ms` is session-relative, assigned by the caller (the
    `MentorAgent` event handler). The router does not re-stamp it on
    receipt because the candidate's "t when this fired" is the
    semantically correct anchor for the snapshot row, not "t when the
    dispatch loop happened to drain the pending list."
    """

    type: EventType
    t_ms: int
    payload: dict[str, Any] = field(default_factory=dict)


__all__ = ["EventType", "RouterEvent"]
