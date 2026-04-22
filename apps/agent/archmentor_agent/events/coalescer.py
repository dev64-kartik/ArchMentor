"""Pure event-batch coalescer for the dispatcher.

Merges a list of `RouterEvent` (everything that piled up while a brain
call was in flight) into a single `RouterEvent` so the next brain call
only fires once per coalesced batch.

M2 rule — `turn_end` always wins:

- Any batch containing at least one `TURN_END` collapses to a single
  `TURN_END`. The merged payload exposes `transcripts` as the ordered
  list of every `TURN_END` payload's `text` (or the full payload if
  `text` is absent), so the brain sees every utterance in order.
- A batch of only `LONG_SILENCE` returns the most recent one (latest
  `t_ms`); same for `PHASE_TIMER`. If both `LONG_SILENCE` and
  `PHASE_TIMER` are present without any `TURN_END`, the latest event
  by `t_ms` wins (priority is undefined for a no-speech batch in M2).
- `t_ms` on the merged event is the latest `t_ms` across the batch —
  monotonic-by-construction with the next dispatch's pre-await stamp.

Assumption flagged for M3: `canvas_change` introduces a priority field
because a factual error drawn mid-speech is more urgent than the
current `turn_end`. The router rejects `canvas_change` at `handle()`
entry today, so this function never sees one.

Pure function. No I/O, no logging — the router logs the merge result
once per dispatch.
"""

from __future__ import annotations

from typing import Any

from archmentor_agent.events.types import EventType, RouterEvent


def coalesce(events: list[RouterEvent]) -> RouterEvent:
    """Collapse a non-empty batch into a single `RouterEvent`.

    Raises `ValueError` if the batch is empty — callers must guard
    upstream (the router only invokes us when `pending` had items).
    Raises `ValueError` if the batch contains a `CANVAS_CHANGE`
    (the router rejects it at `handle()` entry; defense in depth).
    """
    if not events:
        raise ValueError("coalesce requires a non-empty batch")
    for ev in events:
        if ev.type is EventType.CANVAS_CHANGE:
            raise ValueError(
                "canvas_change must be rejected at router.handle() entry; "
                "the coalescer must never see one in M2."
            )

    turn_ends = [e for e in events if e.type is EventType.TURN_END]
    if turn_ends:
        merged_payload: dict[str, Any] = {
            "transcripts": [_extract_transcript(e.payload) for e in turn_ends],
            # Surface the merged-from list so the brain prompt and snapshots
            # can show what got coalesced; M3 will replace this with a
            # priority-aware merge log.
            "merged_from": [e.type.value for e in events],
        }
        latest = max(events, key=lambda e: e.t_ms)
        return RouterEvent(
            type=EventType.TURN_END,
            t_ms=latest.t_ms,
            payload=merged_payload,
        )

    # No turn_end → the latest event wins. M2 has only LONG_SILENCE and
    # PHASE_TIMER landing here; payload comes through verbatim.
    latest = max(events, key=lambda e: e.t_ms)
    return RouterEvent(
        type=latest.type,
        t_ms=latest.t_ms,
        payload={**latest.payload, "merged_from": [e.type.value for e in events]},
    )


def _extract_transcript(payload: dict[str, Any]) -> Any:
    """Pull a transcript-shaped value out of a `TURN_END` payload.

    Falls back to the whole payload if `text` is absent so unusual
    shapes (e.g. a `transcripts` list pre-merged upstream) still
    survive the round-trip.
    """
    if "text" in payload:
        return payload["text"]
    return payload


__all__ = ["coalesce"]
