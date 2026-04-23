"""Tests for the utterance queue: TTL drops, turn-invalidation, on_stale."""

from __future__ import annotations

from archmentor_agent.queue import UtteranceQueue
from archmentor_agent.state.session_state import PendingUtterance


class _FakeClock:
    """Deterministic monotonic clock for queue freshness checks."""

    def __init__(self, t0_ms: int = 0) -> None:
        self.t = t0_ms

    def now(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


def _utt(generated_at_ms: int, text: str = "hi", ttl_ms: int = 10_000) -> PendingUtterance:
    return PendingUtterance(text=text, generated_at_ms=generated_at_ms, ttl_ms=ttl_ms)


def test_push_then_pop_returns_item() -> None:
    clock = _FakeClock(t0_ms=1_000)
    queue = UtteranceQueue(clock.now)
    queue.push(_utt(generated_at_ms=1_000, text="hello"))

    popped = queue.pop_if_fresh()

    assert popped is not None
    assert popped.text == "hello"
    assert len(queue) == 0


def test_pop_drops_expired_and_returns_none() -> None:
    clock = _FakeClock(t0_ms=1_000)
    drops: list[PendingUtterance] = []
    queue = UtteranceQueue(clock.now, ttl_ms=10_000, on_stale=drops.append)
    queue.push(_utt(generated_at_ms=1_000))

    clock.advance(15_000)

    assert queue.pop_if_fresh() is None
    assert len(drops) == 1
    assert drops[0].generated_at_ms == 1_000


def test_pop_skips_stale_and_returns_first_fresh() -> None:
    clock = _FakeClock(t0_ms=1_000)
    drops: list[PendingUtterance] = []
    queue = UtteranceQueue(clock.now, ttl_ms=10_000, on_stale=drops.append)
    # Two stale, one fresh, one fresh.
    queue.push(_utt(generated_at_ms=1_000, text="stale-1"))
    queue.push(_utt(generated_at_ms=1_500, text="stale-2"))
    clock.advance(15_000)
    queue.push(_utt(generated_at_ms=clock.now(), text="fresh"))
    queue.push(_utt(generated_at_ms=clock.now(), text="fresh-2"))

    popped = queue.pop_if_fresh()

    assert popped is not None
    assert popped.text == "fresh"
    assert [u.text for u in drops] == ["stale-1", "stale-2"]
    # Second fresh remains.
    assert len(queue) == 1


def test_pop_empty_returns_none() -> None:
    queue = UtteranceQueue(_FakeClock().now)
    assert queue.pop_if_fresh() is None


def test_clear_stale_on_new_turn_drops_older_items() -> None:
    clock = _FakeClock(t0_ms=2_000)
    drops: list[PendingUtterance] = []
    queue = UtteranceQueue(clock.now, on_stale=drops.append)
    queue.push(_utt(generated_at_ms=1_000, text="from-old-turn"))

    dropped_count = queue.clear_stale_on_new_turn(turn_t_ms=1_200)

    assert dropped_count == 1
    assert len(queue) == 0
    assert queue.pop_if_fresh() is None
    assert len(drops) == 1


def test_clear_stale_preserves_newer_than_turn() -> None:
    clock = _FakeClock(t0_ms=2_000)
    queue = UtteranceQueue(clock.now)
    queue.push(_utt(generated_at_ms=1_500, text="newer"))

    dropped = queue.clear_stale_on_new_turn(turn_t_ms=1_200)

    assert dropped == 0
    popped = queue.pop_if_fresh()
    assert popped is not None
    assert popped.text == "newer"


def test_clear_stale_mixed_batch() -> None:
    clock = _FakeClock(t0_ms=3_000)
    queue = UtteranceQueue(clock.now)
    queue.push(_utt(generated_at_ms=1_000, text="dropped"))
    queue.push(_utt(generated_at_ms=2_500, text="kept"))
    queue.push(_utt(generated_at_ms=900, text="dropped-2"))

    dropped = queue.clear_stale_on_new_turn(turn_t_ms=2_000)

    assert dropped == 2
    assert len(queue) == 1
    popped = queue.pop_if_fresh()
    assert popped is not None
    assert popped.text == "kept"


def test_pop_does_not_emit_on_stale_for_fresh_items() -> None:
    clock = _FakeClock(t0_ms=1_000)
    drops: list[PendingUtterance] = []
    queue = UtteranceQueue(clock.now, on_stale=drops.append)
    queue.push(_utt(generated_at_ms=1_000))

    queue.pop_if_fresh()

    assert drops == []
