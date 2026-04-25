"""Event taxonomy for the brain dispatcher.

`RouterEvent` is the only shape the router consumes. M2 wired
`turn_end`, `long_silence`, and `phase_timer`; M3 wires `canvas_change`
and adds a `priority` field so a factual error drawn mid-speech can
preempt the in-flight turn end. Later-milestone event types
(session_start, wrapup_timer, session_end) are intentionally NOT
declared here — they'll land alongside the code that consumes them so
an accidental dispatch can't silently fall through a handler-less
enum value.

`payload` is intentionally typed as `dict[str, Any]`. Each event type
puts a different shape inside (transcript text, silence duration,
phase id, etc.) and constraining it would force a Union the router
doesn't actually inspect — the brain reads `payload` opaquely as part
of the `messages[0]` user turn.

Priority semantics are coalescer-facing, not router-routing-facing:
- HIGH (canvas_change) preempts MEDIUM in a coalesced batch and folds
  any concurrent TURN_END text into `concurrent_transcripts`.
- MEDIUM (turn_end, long_silence) follows the M2 turn_end-wins rule
  inside its tier.
- LOW (phase_timer) only survives a batch where nothing else fired.

`default_priority` is the source of truth — call sites that need the
type-implied priority can derive it without restating the table.
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


class Priority(StrEnum):
    """Coalescer-facing urgency tier. See module docstring."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Explicit rank so we don't lean on enum-declaration order — a future
# reorder of `Priority` values must not silently flip the merge rule.
PRIORITY_RANK: dict[Priority, int] = {
    Priority.LOW: 0,
    Priority.MEDIUM: 1,
    Priority.HIGH: 2,
}


def default_priority(event_type: EventType) -> Priority:
    """Return the default `Priority` tier for an event type.

    `RouterEvent.priority` defaults to MEDIUM so existing M2 fixtures
    that don't pass priority keep working. Call sites that produce a
    canvas_change should pass `priority=default_priority(...)` (or the
    explicit Priority value) so the tier is documented at the source.
    """
    if event_type is EventType.CANVAS_CHANGE:
        return Priority.HIGH
    if event_type is EventType.PHASE_TIMER:
        return Priority.LOW
    # turn_end + long_silence — the M2 default tier.
    return Priority.MEDIUM


@dataclass(frozen=True, slots=True)
class RouterEvent:
    """One event handed to `EventRouter.handle(...)`.

    `t_ms` is session-relative, assigned by the caller (the
    `MentorAgent` event handler). The router does not re-stamp it on
    receipt because the candidate's "t when this fired" is the
    semantically correct anchor for the snapshot row, not "t when the
    dispatch loop happened to drain the pending list."

    `priority` defaults to MEDIUM so M2-era call sites keep their
    semantics; M3 canvas wiring sets HIGH explicitly at the call site.
    """

    type: EventType
    t_ms: int
    payload: dict[str, Any] = field(default_factory=dict)
    priority: Priority = Priority.MEDIUM


__all__ = [
    "PRIORITY_RANK",
    "EventType",
    "Priority",
    "RouterEvent",
    "default_priority",
]
