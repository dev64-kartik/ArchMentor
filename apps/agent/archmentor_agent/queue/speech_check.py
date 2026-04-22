"""Single-source-of-truth flag for "is the candidate currently speaking?"

The router asks this gate before allowing the utterance queue to push
brain output to TTS. The candidate-speaking signal comes from the
livekit-agents `user_input_transcribed` interim/final stream — the
gate exposes `mark_speaking()` / `mark_done_speaking()` for the
`MentorAgent` event handler to call explicitly.

Why explicit marks rather than passively listening to the framework
event bus: the gate is intentionally decoupled from the livekit event
shape so it stays unit-testable without spinning up a livekit harness.
The handler in `MentorAgent` is the single integration point.

After `mark_done_speaking()` the gate keeps reporting "speaking" for
`grace_ms` (default 250 ms). VAD's end-of-turn signal occasionally
arrives a beat before the candidate has actually finished — playing
TTS on top of that tail produces an audible barge over the candidate's
last syllable. The grace window absorbs the jitter without waiting on
a follow-up VAD bounce.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

log = structlog.get_logger(__name__)

NowMs = Callable[[], int]

DEFAULT_GRACE_MS = 250


class SpeechCheckGate:
    """Tracks candidate-speaking state for the utterance pipeline."""

    def __init__(self, now_ms: NowMs, *, grace_ms: int = DEFAULT_GRACE_MS) -> None:
        self._now_ms = now_ms
        self._grace_ms = grace_ms
        self._speaking: bool = False
        # Set when `mark_done_speaking()` is called; until `now - _done_at_ms
        # > _grace_ms`, `is_candidate_speaking()` still returns True.
        self._done_at_ms: int | None = None

    def mark_speaking(self) -> None:
        if not self._speaking:
            log.info("gate.mark_speaking")
        self._speaking = True
        self._done_at_ms = None

    def mark_done_speaking(self) -> None:
        if self._speaking:
            log.info("gate.mark_done_speaking", grace_ms=self._grace_ms)
        self._speaking = False
        self._done_at_ms = self._now_ms()

    def is_candidate_speaking(self) -> bool:
        if self._speaking:
            return True
        if self._done_at_ms is None:
            return False
        if self._now_ms() - self._done_at_ms <= self._grace_ms:
            return True
        # Grace expired — clear the latch so future callers don't recompute.
        self._done_at_ms = None
        return False


__all__ = ["DEFAULT_GRACE_MS", "NowMs", "SpeechCheckGate"]
