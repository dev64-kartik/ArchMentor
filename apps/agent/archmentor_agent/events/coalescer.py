"""Pure event-batch coalescer for the dispatcher.

Merges a list of `RouterEvent` (everything that piled up while a brain
call was in flight) into a single `RouterEvent` so the next brain call
only fires once per coalesced batch.

M3 priority semantics:

- Any HIGH (e.g. canvas_change): latest HIGH event's type; payload is
  the HIGH event's source payload + `concurrent_transcripts` (any
  TURN_END text in the batch) + `merged_from`.
- MEDIUM only, with TURN_END: collapses to a single TURN_END whose
  payload carries `transcripts` (every TURN_END's text in order) +
  `merged_from`. M2 rule preserved.
- MEDIUM only, no TURN_END: latest by `t_ms`; source payload pass-through
  + `merged_from`. M2 rule preserved.
- LOW only: latest by `t_ms`; source payload pass-through + `merged_from`.

`t_ms` on the merged event is the latest `t_ms` across the batch —
monotonic-by-construction with the next dispatch's pre-await stamp.
This carries the M2 invariant forward so the router's I3 invariant
(snapshot t_ms is the moment the batch closed) still holds.

`concurrent_transcripts` is empty when no TURN_END is in the batch,
*including* a batch of one CANVAS_CHANGE. The brain prompt's
`[Event payload shapes]` section (Unit 9 lands the bootstrap.py
clause) documents this contract; the offline contract test in
`test_event_coalescer.py` verifies the coalescer output matches.

Pure function. No I/O, no logging — the router logs the merge result
once per dispatch.
"""

from __future__ import annotations

from typing import Any

from archmentor_agent.events.types import (
    PRIORITY_RANK,
    EventType,
    Priority,
    RouterEvent,
)


def coalesce(events: list[RouterEvent]) -> RouterEvent:
    """Collapse a non-empty batch into a single `RouterEvent`.

    Raises `ValueError` if the batch is empty — callers must guard
    upstream (the router only invokes us when `pending` had items).
    """
    if not events:
        raise ValueError("coalesce requires a non-empty batch")

    max_rank = max(PRIORITY_RANK[e.priority] for e in events)
    latest_t_ms = max(e.t_ms for e in events)
    merged_from = [e.type.value for e in events]

    if max_rank == PRIORITY_RANK[Priority.HIGH]:
        # Highest tier: a HIGH event always wins. If multiple HIGH
        # events landed in the same batch, the latest by `t_ms` carries
        # the merged payload. Any TURN_END text in the batch folds in
        # via `concurrent_transcripts` so speech-while-drawing stays
        # visible to the brain.
        high_events = [e for e in events if e.priority is Priority.HIGH]
        latest_high = max(high_events, key=lambda e: e.t_ms)
        concurrent_transcripts = [
            _extract_transcript(e.payload) for e in events if e.type is EventType.TURN_END
        ]
        merged_payload: dict[str, Any] = {
            **latest_high.payload,
            "concurrent_transcripts": concurrent_transcripts,
            "merged_from": merged_from,
        }
        return RouterEvent(
            type=latest_high.type,
            t_ms=latest_t_ms,
            payload=merged_payload,
            priority=latest_high.priority,
        )

    # M2 turn_end-wins rule: any batch containing at least one TURN_END
    # collapses to a single TURN_END with all transcripts in order.
    turn_ends = [e for e in events if e.type is EventType.TURN_END]
    if turn_ends:
        merged_payload = {
            "transcripts": [_extract_transcript(e.payload) for e in turn_ends],
            "merged_from": merged_from,
        }
        return RouterEvent(
            type=EventType.TURN_END,
            t_ms=latest_t_ms,
            payload=merged_payload,
            priority=Priority.MEDIUM,
        )

    # No HIGH, no TURN_END — latest by `t_ms` wins. M2 LONG_SILENCE +
    # PHASE_TIMER batches still resolve here.
    latest = max(events, key=lambda e: e.t_ms)
    return RouterEvent(
        type=latest.type,
        t_ms=latest.t_ms,
        payload={**latest.payload, "merged_from": merged_from},
        priority=latest.priority,
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
