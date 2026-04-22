"""Tests for the speech-check gate: explicit marks + grace window."""

from __future__ import annotations

from archmentor_agent.queue import SpeechCheckGate


class _FakeClock:
    def __init__(self, t0_ms: int = 0) -> None:
        self.t = t0_ms

    def now(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


def test_default_state_not_speaking() -> None:
    gate = SpeechCheckGate(_FakeClock().now)
    assert gate.is_candidate_speaking() is False


def test_mark_speaking_sets_state() -> None:
    gate = SpeechCheckGate(_FakeClock().now)
    gate.mark_speaking()
    assert gate.is_candidate_speaking() is True


def test_grace_window_keeps_speaking_after_done() -> None:
    clock = _FakeClock(t0_ms=1_000)
    gate = SpeechCheckGate(clock.now, grace_ms=250)
    gate.mark_speaking()
    gate.mark_done_speaking()

    # Within grace.
    clock.advance(100)
    assert gate.is_candidate_speaking() is True

    # Right at the boundary still in (<=).
    clock.advance(150)
    assert gate.is_candidate_speaking() is True

    # Past grace.
    clock.advance(1)
    assert gate.is_candidate_speaking() is False


def test_grace_clears_after_first_expired_check() -> None:
    """Once the grace expires, subsequent checks short-circuit on `_done_at_ms`."""
    clock = _FakeClock(t0_ms=1_000)
    gate = SpeechCheckGate(clock.now, grace_ms=100)
    gate.mark_speaking()
    gate.mark_done_speaking()

    clock.advance(500)
    assert gate.is_candidate_speaking() is False
    # No clock advance — internal latch should already be cleared.
    assert gate.is_candidate_speaking() is False


def test_mark_speaking_clears_pending_grace() -> None:
    """A new mark_speaking after mark_done_speaking should reset the gate.

    Otherwise a quick interim → final → interim sequence in the grace
    window would short-circuit to False the moment grace expired,
    even though the candidate is mid-speech again.
    """
    clock = _FakeClock(t0_ms=1_000)
    gate = SpeechCheckGate(clock.now, grace_ms=250)
    gate.mark_speaking()
    gate.mark_done_speaking()

    clock.advance(50)  # still in grace
    gate.mark_speaking()  # candidate spoke again

    # Advance past the original grace window — gate should still be True
    # because the new mark_speaking superseded the prior done timestamp.
    clock.advance(500)
    assert gate.is_candidate_speaking() is True


def test_mark_done_without_prior_speaking_still_triggers_grace() -> None:
    """Defensive: if framework emits a final without an interim, treat the
    final as the boundary so the next 250ms is still 'speaking' (we just
    received the candidate's words)."""
    clock = _FakeClock(t0_ms=1_000)
    gate = SpeechCheckGate(clock.now, grace_ms=250)

    gate.mark_done_speaking()
    assert gate.is_candidate_speaking() is True

    clock.advance(300)
    assert gate.is_candidate_speaking() is False
