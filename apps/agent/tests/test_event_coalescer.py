"""Tests for the M2 coalescer: turn_end-wins, latest-payload, canvas guard."""

from __future__ import annotations

import pytest
from archmentor_agent.events import EventType, RouterEvent, coalesce


def _ev(type_: EventType, t_ms: int, **payload: object) -> RouterEvent:
    return RouterEvent(type=type_, t_ms=t_ms, payload=dict(payload))


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
    batch = [
        _ev(EventType.LONG_SILENCE, t_ms=900),
        _ev(EventType.TURN_END, t_ms=1_500, text="hello"),
        _ev(EventType.LONG_SILENCE, t_ms=1_000),
    ]
    merged = coalesce(batch)
    assert merged.type is EventType.TURN_END
    assert merged.t_ms == 1_500
    assert merged.payload["transcripts"] == ["hello"]
    assert merged.payload["merged_from"] == [
        "long_silence",
        "turn_end",
        "long_silence",
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
    assert merged.t_ms == 3_000  # latest


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


def test_only_phase_timer_returns_latest() -> None:
    batch = [_ev(EventType.PHASE_TIMER, t_ms=600, phase="hld")]
    merged = coalesce(batch)
    assert merged.type is EventType.PHASE_TIMER
    assert merged.payload["phase"] == "hld"


def test_canvas_change_in_batch_rejected_with_value_error() -> None:
    """canvas_change is rejected at handle() entry; coalescer is the
    second line of defense — if one slips through, fail loudly."""
    with pytest.raises(ValueError, match="canvas_change"):
        coalesce([_ev(EventType.CANVAS_CHANGE, t_ms=100)])


def test_turn_end_payload_without_text_falls_back_to_full_payload() -> None:
    merged = coalesce(
        [
            _ev(EventType.TURN_END, t_ms=100, segments=[{"text": "a"}]),
        ]
    )
    assert merged.payload["transcripts"] == [{"segments": [{"text": "a"}]}]
