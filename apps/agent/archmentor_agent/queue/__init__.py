"""Utterance queue + speech-check gate.

Brain-generated utterances pass through a speech-check (is candidate
currently speaking?) before hitting `session.say()`. If the candidate is
mid-speech, the utterance is queued with a 10s TTL and delivered at the
next VAD pause. Stale utterances are dropped and logged to
`brain_snapshots`.

No separate Haiku relevance check — Opus already made the relevance
decision when it generated the utterance.

Implementation lands in M2.
"""
