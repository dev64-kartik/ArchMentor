"""Utterance queue + speech-check gate.

Brain-generated utterances pass through a speech-check (is candidate
currently speaking?) before hitting `session.say()`. If the candidate is
mid-speech, the utterance is queued with a 10s TTL and delivered at the
next VAD pause. Stale utterances are dropped and logged.

No separate Haiku relevance check — Opus already made the relevance
decision when it generated the utterance.
"""

from archmentor_agent.queue.speech_check import (
    DEFAULT_GRACE_MS,
    SpeechCheckGate,
)
from archmentor_agent.queue.utterance_queue import (
    DEFAULT_TTL_MS,
    UtteranceQueue,
)

__all__ = [
    "DEFAULT_GRACE_MS",
    "DEFAULT_TTL_MS",
    "SpeechCheckGate",
    "UtteranceQueue",
]
