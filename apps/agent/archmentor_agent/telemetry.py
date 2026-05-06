"""Per-session in-memory counters surfaced at shutdown (M4 Unit 5 / R4).

Six fields cover the M4 dogfood-gate signal: time-to-first-audio
distribution, brain calls made, throttle effectiveness (idempotent +
cooldown skips), queue staleness drops, and compaction frequency.
Persistence is deferred — no Postgres column, no Alembic migration, no
`SESSION_TELEMETRY` ledger event. M5/M6 add storage when concrete
consumers exist; until then the agent emits one
``agent.session.telemetry`` structured log line on `MentorAgent.shutdown`
and replay/dogfood gates parse the log.

Concurrency invariant (binding): every increment site MUST run on the
asyncio event-loop thread. Built-in ``int += 1`` is bytecode-non-atomic
and would tear if invoked from a thread-pool executor. The documented
hooks (router ``_dispatch``, ``_StreamingTtsHandle._on_delta``, queue
``on_stale`` callback, ``MentorAgent._run_compaction``) are all
event-loop-resident; ``_assert_event_loop_thread`` catches any future
regression that pushes an increment into ``run_in_executor``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from typing import Any


def _assert_event_loop_thread() -> None:
    """Raise if called from a thread without a running asyncio loop.

    `asyncio.get_running_loop()` raises ``RuntimeError`` when invoked
    from a thread that does not own the loop (e.g. from a thread-pool
    executor). The increment sites enumerated in the module docstring
    are all event-loop-resident; this assertion is the guardrail
    against a future refactor that drops one of them into
    ``run_in_executor`` where ``int += 1`` would tear under contention.

    Intentionally cheap: the function is called on every counter
    increment, so the overhead is the cost of one C-level loop probe.
    """
    asyncio.get_running_loop()


@dataclass(slots=True)
class SessionTelemetry:
    """Six per-session counters surfaced at session-end.

    Field semantics:

    - ``ttfa_ms_histogram`` — raw time-to-first-audio samples in
      milliseconds; one entry per dispatch where the streaming brain
      delivered at least one non-empty utterance delta. P50 / P95 / max
      are computed at digest time so the dogfood gate can pivot the
      bucket boundaries without rewriting the schema.
    - ``brain_calls_made`` — incremented in
      ``EventRouter._dispatch`` after ``decide()`` returns, regardless
      of decision kind (including router-side skips and cost-cap
      short-circuits). Captures the throttle's effectiveness as the
      ratio of skipped paths to total dispatches.
    - ``skipped_idempotent_count`` — incremented in ``_dispatch`` when
      the fingerprint short-circuits to
      ``BrainDecision.skipped_idempotent()``.
    - ``skipped_cooldown_count`` — incremented in ``_dispatch`` when
      the cooldown gate short-circuits to
      ``BrainDecision.skipped_cooldown(...)``.
    - ``dropped_stale_count`` — incremented from the
      ``UtteranceQueue.on_stale`` callback (per drop), covering both
      TTL drops and the new-turn supersede path.
    - ``compactions_run`` — incremented at the top of
      ``MentorAgent._run_compaction`` immediately after
      ``_summary_in_flight`` flips True. Counts compactor ATTEMPTS
      (the threshold fired and we entered the body), not Haiku calls
      billed. Failed compactions are separately observable via
      ``agent.summary.compaction.failed`` log lines.
    """

    ttfa_ms_histogram: list[int] = field(default_factory=list)
    brain_calls_made: int = 0
    skipped_idempotent_count: int = 0
    skipped_cooldown_count: int = 0
    dropped_stale_count: int = 0
    compactions_run: int = 0

    def record_ttfa_ms(self, ttfa_ms: int) -> None:
        """Append one time-to-first-audio sample.

        Called from ``_StreamingTtsHandle._on_delta`` on the first
        non-empty delta of a streaming dispatch.
        """
        _assert_event_loop_thread()
        self.ttfa_ms_histogram.append(ttfa_ms)

    def record_brain_call(self) -> None:
        _assert_event_loop_thread()
        self.brain_calls_made += 1

    def record_skipped_idempotent(self) -> None:
        _assert_event_loop_thread()
        self.skipped_idempotent_count += 1

    def record_skipped_cooldown(self) -> None:
        _assert_event_loop_thread()
        self.skipped_cooldown_count += 1

    def record_dropped_stale(self) -> None:
        _assert_event_loop_thread()
        self.dropped_stale_count += 1

    def record_compaction(self) -> None:
        _assert_event_loop_thread()
        self.compactions_run += 1

    def as_log_payload(self) -> dict[str, Any]:
        """Return a structured-log-friendly dict for ``log.info(**...)``.

        ``dataclasses.asdict`` recursively copies ``ttfa_ms_histogram``,
        which is what we want — the structured logger emits the raw
        list and the dogfood gate computes percentiles downstream.
        """
        return dataclasses.asdict(self)


__all__ = ["SessionTelemetry", "_assert_event_loop_thread"]
