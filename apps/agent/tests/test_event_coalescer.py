"""Tests for the priority-aware coalescer.

M3 promotes `canvas_change` from "router-rejected" to "highest-priority
preempts the in-flight turn end". The coalescer's job: collapse a
mixed-tier batch into one `RouterEvent` whose payload carries every
signal the brain needs (canvas description + any concurrent transcript
text + the merge-source list).

Each case is grouped by what the brain ends up seeing — the source
payload semantics, not the priority machinery, are what must not drift.
"""

from __future__ import annotations

import pytest
from archmentor_agent.events import (
    EventType,
    Priority,
    RouterEvent,
    coalesce,
    default_priority,
)


def _ev(
    type_: EventType,
    t_ms: int,
    *,
    priority: Priority | None = None,
    **payload: object,
) -> RouterEvent:
    """Build a RouterEvent with the type-implied priority by default."""
    resolved = priority if priority is not None else default_priority(type_)
    return RouterEvent(type=type_, t_ms=t_ms, payload=dict(payload), priority=resolved)


# ---------------------------------------------------------------------------
# M2 regression — non-canvas batches must not change semantics.
# ---------------------------------------------------------------------------


def test_empty_batch_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        coalesce([])


def test_single_turn_end_passes_through() -> None:
    merged = coalesce([_ev(EventType.TURN_END, t_ms=1_000, text="hello")])
    assert merged.type is EventType.TURN_END
    assert merged.t_ms == 1_000
    assert merged.payload["transcripts"] == ["hello"]
    assert merged.payload["merged_from"] == ["turn_end"]


def test_turn_end_wins_against_long_silence() -> None:
    """M2 mix (TURN_END + LONG_SILENCE + PHASE_TIMER): TURN_END wins."""
    batch = [
        _ev(EventType.LONG_SILENCE, t_ms=900),
        _ev(EventType.TURN_END, t_ms=1_500, text="hello"),
        _ev(EventType.LONG_SILENCE, t_ms=1_000),
        _ev(EventType.PHASE_TIMER, t_ms=1_400, phase="hld"),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.TURN_END
    assert merged.t_ms == 1_500
    assert merged.payload["transcripts"] == ["hello"]
    assert merged.payload["merged_from"] == [
        "long_silence",
        "turn_end",
        "long_silence",
        "phase_timer",
    ]


def test_multiple_turn_ends_collect_in_order() -> None:
    batch = [
        _ev(EventType.TURN_END, t_ms=1_000, text="first"),
        _ev(EventType.TURN_END, t_ms=2_000, text="second"),
        _ev(EventType.TURN_END, t_ms=3_000, text="third"),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.TURN_END
    assert merged.payload["transcripts"] == ["first", "second", "third"]
    assert merged.t_ms == 3_000


def test_only_long_silence_returns_latest() -> None:
    batch = [
        _ev(EventType.LONG_SILENCE, t_ms=1_000, duration_s=5),
        _ev(EventType.LONG_SILENCE, t_ms=2_500, duration_s=20),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.LONG_SILENCE
    assert merged.t_ms == 2_500
    assert merged.payload["duration_s"] == 20
    assert merged.payload["merged_from"] == ["long_silence", "long_silence"]


def test_phase_timer_plus_long_silence_returns_latest() -> None:
    """PHASE_TIMER + LONG_SILENCE without TURN_END: latest wins.

    LONG_SILENCE is MEDIUM and PHASE_TIMER is LOW, so the highest
    priority tier in the batch is MEDIUM — which falls through to the
    M2 latest-by-t_ms branch since no TURN_END is present.
    """
    batch = [
        _ev(EventType.PHASE_TIMER, t_ms=1_000, phase="hld"),
        _ev(EventType.LONG_SILENCE, t_ms=900, duration_s=5),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.PHASE_TIMER
    assert merged.t_ms == 1_000


def test_turn_end_payload_without_text_falls_back_to_full_payload() -> None:
    merged = coalesce(
        [
            _ev(EventType.TURN_END, t_ms=100, segments=[{"text": "a"}]),
        ]
    )
    assert merged.payload["transcripts"] == [{"segments": [{"text": "a"}]}]


# ---------------------------------------------------------------------------
# M3 priority-aware behaviour.
# ---------------------------------------------------------------------------


def test_default_priority_canvas_change_is_high() -> None:
    """The HIGH-tier mapping for canvas_change is the load-bearing
    invariant for preemption. A future contributor who flips this to
    MEDIUM silently regresses the entire M3 priority story."""
    assert default_priority(EventType.CANVAS_CHANGE) is Priority.HIGH


def test_default_priority_turn_end_is_medium() -> None:
    assert default_priority(EventType.TURN_END) is Priority.MEDIUM


def test_default_priority_long_silence_is_medium() -> None:
    assert default_priority(EventType.LONG_SILENCE) is Priority.MEDIUM


def test_default_priority_phase_timer_is_low() -> None:
    assert default_priority(EventType.PHASE_TIMER) is Priority.LOW


def test_router_event_default_priority_is_medium() -> None:
    """Existing M2 fixtures construct `RouterEvent(...)` without
    passing priority. They must keep their MEDIUM tier so the M2
    turn_end-wins rule doesn't silently regress."""
    event = RouterEvent(type=EventType.TURN_END, t_ms=0)
    assert event.priority is Priority.MEDIUM


def test_single_canvas_change_passes_through_with_empty_concurrent_transcripts() -> None:
    """A solo CANVAS_CHANGE keeps its source payload and adds an
    empty `concurrent_transcripts` so the brain prompt's contract
    holds regardless of whether speech happened to be in flight."""
    merged = coalesce(
        [
            _ev(
                EventType.CANVAS_CHANGE,
                t_ms=2_000,
                scene_text="Components: <label>API Gateway</label>",
                scene_fingerprint="abc",
            )
        ]
    )
    assert merged.type is EventType.CANVAS_CHANGE
    assert merged.t_ms == 2_000
    assert merged.priority is Priority.HIGH
    assert merged.payload["scene_text"] == "Components: <label>API Gateway</label>"
    assert merged.payload["scene_fingerprint"] == "abc"
    assert merged.payload["concurrent_transcripts"] == []
    assert merged.payload["merged_from"] == ["canvas_change"]


def test_canvas_change_wins_against_turn_end_in_same_batch() -> None:
    """The headline M3 rule: a factual error drawn mid-speech
    preempts the speech itself — but the speech's transcript still
    folds into the merged payload so the brain doesn't lose context."""
    batch = [
        _ev(EventType.TURN_END, t_ms=1_000, text="and then we'll partition by user_id"),
        _ev(
            EventType.CANVAS_CHANGE,
            t_ms=1_500,
            scene_text="Components: <label>API</label>",
        ),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.CANVAS_CHANGE
    assert merged.t_ms == 1_500
    assert merged.priority is Priority.HIGH
    assert merged.payload["scene_text"] == "Components: <label>API</label>"
    assert merged.payload["concurrent_transcripts"] == ["and then we'll partition by user_id"]
    assert merged.payload["merged_from"] == ["turn_end", "canvas_change"]


def test_two_canvas_change_events_latest_by_t_ms_wins() -> None:
    """Same-tier ties resolve by `t_ms` — the most recent scene
    represents the candidate's current intent."""
    batch = [
        _ev(
            EventType.CANVAS_CHANGE,
            t_ms=1_000,
            scene_text="Components: <label>old</label>",
        ),
        _ev(
            EventType.CANVAS_CHANGE,
            t_ms=2_500,
            scene_text="Components: <label>new</label>",
        ),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.CANVAS_CHANGE
    assert merged.t_ms == 2_500
    assert merged.payload["scene_text"] == "Components: <label>new</label>"
    assert merged.payload["concurrent_transcripts"] == []


def test_canvas_change_preempts_turn_end_plus_long_silence() -> None:
    """A mixed batch with HIGH + MEDIUM (TURN_END) + MEDIUM (LONG_SILENCE)
    still routes through HIGH; transcript text folds in; long-silence
    semantics are absorbed via `merged_from` only."""
    batch = [
        _ev(EventType.LONG_SILENCE, t_ms=800, duration_s=5),
        _ev(EventType.TURN_END, t_ms=900, text="give me a second"),
        _ev(
            EventType.CANVAS_CHANGE,
            t_ms=1_500,
            scene_text="Components: <label>cache</label>",
        ),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.CANVAS_CHANGE
    assert merged.payload["concurrent_transcripts"] == ["give me a second"]
    assert merged.payload["merged_from"] == [
        "long_silence",
        "turn_end",
        "canvas_change",
    ]


# ---------------------------------------------------------------------------
# R20 contract test — coalescer output matches the documented brain-prompt
# payload shape. Unit 9 ties this to bootstrap.py's `[Event payload shapes]`
# section; for now the keys are asserted directly so a coalescer drift
# fails CI before the brain ever sees the regression.
# ---------------------------------------------------------------------------


def test_coalesced_canvas_change_payload_matches_documented_keys() -> None:
    """R20: the brain prompt documents `canvas_change` as carrying
    `scene_text`, `concurrent_transcripts`, `scene_fingerprint`, and
    `merged_from`. The coalescer must emit exactly those keys (plus any
    source-payload keys that pass through unchanged).
    """
    expected_keys = {
        "scene_text",
        "concurrent_transcripts",
        "scene_fingerprint",
        "merged_from",
    }
    batch = [
        _ev(EventType.TURN_END, t_ms=1_000, text="hi"),
        _ev(
            EventType.CANVAS_CHANGE,
            t_ms=1_500,
            scene_text="Components: <label>API</label>",
            scene_fingerprint="sha256:abc",
        ),
    ]
    merged = coalesce(batch)
    assert expected_keys.issubset(merged.payload.keys()), (
        f"coalescer output is missing brain-contract keys: {expected_keys - merged.payload.keys()}"
    )


def test_coalesced_turn_end_payload_matches_documented_keys() -> None:
    """R20: the brain prompt documents `turn_end` as carrying
    `transcripts` + `merged_from`. The M2 rule must keep this shape."""
    expected_keys = {"transcripts", "merged_from"}
    merged = coalesce(
        [
            _ev(EventType.TURN_END, t_ms=1_000, text="a"),
            _ev(EventType.TURN_END, t_ms=1_100, text="b"),
        ]
    )
    assert expected_keys.issubset(merged.payload.keys())
