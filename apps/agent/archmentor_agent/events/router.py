"""Serialized event router.

Only one brain call in flight at a time. Concurrent events
(`turn_end + canvas_change`) are coalesced into a single call with
merged context. In-flight calls are cancelled via AbortController on
candidate speech resume; stale completed calls are discarded by the
utterance queue's TTL gate.

Event types (M1/M2):
- turn_end          — VAD: ~1.5s silence after speech
- long_silence      — No speech for >20s; scaffold probe
- canvas_change     — Excalidraw diff, debounced to 2s
- phase_timer       — Every 2 min; progress check
- session_start     — Static opening utterance, no brain call
- wrapup_timer      — t=40min, t=44min announcements
- session_end       — t=45min or explicit; closing + enqueue report

Implementation lands in M2.
"""

from __future__ import annotations

# Implementation lands in M2.
