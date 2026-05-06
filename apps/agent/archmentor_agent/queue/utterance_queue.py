"""FIFO queue for brain-generated utterances awaiting a TTS slot.

Sits between the event router (which pushes brain decisions that hit
`decision=speak`) and the TTS path. Two freshness rules apply:

- **TTL** — every queued `PendingUtterance` has a generation timestamp
  and a TTL (default 10s). On `pop_if_fresh`, expired items are
  discarded silently and the optional `on_stale` callback fires per drop.
- **Turn invalidation** — when a new candidate turn arrives, any
  utterance generated *before* that turn is dropped. The brain reasoned
  about an older context; speaking it now would reply to stale state.
  TTL is the fallback for idle pauses; turn-arrival is the primary
  freshness signal (see plan Risks row "Stale utterance delivered…").

Single asyncio loop, no locks. The router and `MentorAgent` both call
into this from the same loop.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from archmentor_agent.state.session_state import PendingUtterance

log = structlog.get_logger(__name__)

OnStale = Callable[["PendingUtterance"], None]
NowMs = Callable[[], int]

DEFAULT_TTL_MS = 10_000


class UtteranceQueue:
    """FIFO queue of `PendingUtterance` with TTL + turn-invalidation."""

    def __init__(
        self,
        now_ms: NowMs,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        on_stale: OnStale | None = None,
    ) -> None:
        self._now_ms = now_ms
        self._ttl_ms = ttl_ms
        self._on_stale = on_stale
        self._items: deque[PendingUtterance] = deque()

    def push(self, utterance: PendingUtterance) -> None:
        log.info(
            "queue.push",
            generated_at_ms=utterance.generated_at_ms,
            ttl_ms=utterance.ttl_ms,
            text_len=len(utterance.text),
        )
        self._items.append(utterance)

    def pop_if_fresh(self) -> PendingUtterance | None:
        """Return the next non-stale utterance, dropping stale ones in front.

        Stale = `now_ms - generated_at_ms > ttl_ms`. Each drop fires
        `on_stale(utterance)` so the caller can log to the ledger.
        Returns None if the queue is empty or every queued item is stale.
        """
        now = self._now_ms()
        while self._items:
            head = self._items.popleft()
            if now - head.generated_at_ms > head.ttl_ms:
                log.info(
                    "queue.dropped_stale",
                    generated_at_ms=head.generated_at_ms,
                    age_ms=now - head.generated_at_ms,
                    ttl_ms=head.ttl_ms,
                )
                if self._on_stale is not None:
                    self._on_stale(head)
                continue
            log.info(
                "queue.delivered",
                generated_at_ms=head.generated_at_ms,
                age_ms=now - head.generated_at_ms,
            )
            return head
        return None

    def peek_fresh(self) -> PendingUtterance | None:
        """Return the next non-stale utterance WITHOUT popping it.

        Same TTL gating as ``pop_if_fresh`` — stale items in front are
        dropped (the queue can't hand back a ghost), but the returned
        item stays at the head. Used by the router as a cheap "is
        there a queued speak worth playing?" check before invoking the
        agent's drain callback (Unit 2 / R22).
        """
        now = self._now_ms()
        while self._items:
            head = self._items[0]
            if now - head.generated_at_ms > head.ttl_ms:
                self._items.popleft()
                log.info(
                    "queue.dropped_stale",
                    generated_at_ms=head.generated_at_ms,
                    age_ms=now - head.generated_at_ms,
                    ttl_ms=head.ttl_ms,
                )
                if self._on_stale is not None:
                    self._on_stale(head)
                continue
            return head
        return None

    def bump_ttls(self, extra_ms: int) -> None:
        """Extend every queued item's TTL by ``extra_ms``.

        The router calls this in ``_dispatch``'s ``finally`` block with
        the duration of the brain call so a queued speak doesn't
        expire on the next ``pop_if_fresh`` purely because a competing
        event delayed drain (master plan §697 lever (b); Unit 2 / R23).

        Per-item update via ``model_copy`` — different items entered
        the queue at different times and the right invariant is "every
        item gets the full TTL it would have had if no other event
        had stolen the dispatch slot." Non-positive ``extra_ms`` is a
        no-op so the call site doesn't have to guard.
        """
        if extra_ms <= 0 or not self._items:
            return
        bumped: deque[PendingUtterance] = deque()
        for item in self._items:
            bumped.append(item.model_copy(update={"ttl_ms": item.ttl_ms + extra_ms}))
        self._items = bumped

    def clear_stale_on_new_turn(self, turn_t_ms: int) -> int:
        """Drop every queued utterance generated before `turn_t_ms`.

        Returns the number of utterances dropped. The `on_stale` callback
        fires once per drop with `reason=superseded_by_turn` *not*
        applicable here — `on_stale` carries the utterance only; callers
        differentiate by the `dropped_stale` vs `dropped_superseded`
        log key (the queue logs the latter inline).
        """
        dropped = 0
        survivors: deque[PendingUtterance] = deque()
        while self._items:
            item = self._items.popleft()
            if item.generated_at_ms < turn_t_ms:
                log.info(
                    "queue.dropped_superseded",
                    generated_at_ms=item.generated_at_ms,
                    turn_t_ms=turn_t_ms,
                )
                if self._on_stale is not None:
                    self._on_stale(item)
                dropped += 1
                continue
            survivors.append(item)
        self._items = survivors
        return dropped

    def __len__(self) -> int:
        return len(self._items)


__all__ = ["DEFAULT_TTL_MS", "NowMs", "OnStale", "UtteranceQueue"]
