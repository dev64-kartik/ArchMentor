"""Atomic SessionState I/O against Redis.

Key layout
----------
    session:{id}:state            → JSON-serialized SessionState

No TTL on session keys — explicit cleanup on session end (or via the
stale-session reaper introduced in M6). This prevents state eviction
during pauses, bathroom breaks, or any other natural lull a candidate
might take during a 45-minute interview.

`apply(...)` runs a CAS loop using Redis WATCH/MULTI/EXEC so concurrent
brain dispatches (e.g. a `cancel_in_flight` re-firing during a snapshot
write) can't lose an update. We chose WATCH over a Lua script for M2
because the surface stays small and debuggable; switching to a Lua
script is documented in the M2 plan as a follow-up if WATCH retry
contention shows up in logs.

The `redis` package is a runtime dependency, but we import
`redis.asyncio` lazily inside `_get_redis_module()` so module import
never requires it. `state/redis_store.py` is imported by the agent's
state package early in collection; if `redis.asyncio` were imported at
module top-level, even a `pytest --collect-only` would fail in environ-
ments without the wheel installed (we already pin `redis==7.4.0` in
`pyproject.toml`, so this is mostly belt-and-braces — but it matches
the stt/kokoro lazy-import discipline).
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID

import structlog

from archmentor_agent.config import Settings, get_settings
from archmentor_agent.state.session_state import SessionState

log = structlog.get_logger(__name__)

# Mutator type: receives the current state (None on first write) and
# returns the next state. Sync — the brain call has already happened by
# the time we're inside `apply`; the mutator just merges deltas.
StateMutator = Callable[[SessionState | None], SessionState]


class RedisCasExhaustedError(RuntimeError):
    """`apply(...)` exhausted its CAS retries.

    Surfaced rather than swallowed so the caller (event router) can log
    `state.cas_exhausted` and decide whether to drop the update or fail
    the dispatch. The router currently logs and continues — losing one
    state update is bad but silencing the mentor for the rest of the
    session is worse.
    """


def _state_key(session_id: UUID) -> str:
    return f"session:{session_id}:state"


def _get_redis_module() -> Any:
    """Lazy import of `redis.asyncio`.

    Resolved on first call from any RedisSessionStore method. Mirrors
    `audio/stt.py::_load_model`'s pattern so `pytest` collection on
    machines without a Redis client wheel doesn't blow up at import.
    """
    return importlib.import_module("redis.asyncio")


class RedisSessionStore:
    """Persists `SessionState` atomically across brain dispatches."""

    def __init__(self, redis_url: str, *, client: Any | None = None) -> None:
        # `from_url` returns a Redis client backed by a connection pool.
        # The pool is asyncio-safe for concurrent access from a single
        # event loop, which is all we need — the event router serializes
        # brain dispatches anyway.
        #
        # `client` is the test seam: pass a pre-built fakeredis
        # `FakeAsyncRedis(server=shared_server)` to share state between
        # multiple stores in a CAS test. Production callers omit it.
        self._client = client or _get_redis_module().from_url(
            redis_url,
            decode_responses=True,  # str ↔ str; we JSON-encode ourselves
        )

    async def load(self, session_id: UUID) -> SessionState | None:
        """Read and deserialize. Returns None if the key is absent."""
        raw = await self._client.get(_state_key(session_id))
        if raw is None:
            return None
        return SessionState.model_validate_json(raw)

    async def put(self, session_id: UUID, state: SessionState) -> None:
        """Write the state. No TTL — explicit cleanup on session end.

        This is a non-CAS overwrite intended for `on_enter` initialization
        and tests. Hot-path brain dispatches use `apply(...)` instead so
        concurrent writes don't clobber each other.
        """
        await self._client.set(
            _state_key(session_id),
            state.model_dump_json(),
        )

    async def apply(
        self,
        session_id: UUID,
        mutator: StateMutator,
        *,
        max_retries: int = 3,
    ) -> SessionState:
        """CAS-protected read-modify-write.

        Retries up to `max_retries` times on WatchError (concurrent
        modification by another writer). On exhaustion, raises
        `RedisCasExhaustedError` rather than silently dropping the
        update — losing state without telling the caller masks the
        exact bug we're trying to prevent.

        The mutator must be pure-sync. It receives the deserialized
        current state (None if the key doesn't exist) and returns the
        new state. It MAY be called multiple times across retries, so
        avoid side effects.
        """
        key = _state_key(session_id)
        # `redis.exceptions.WatchError` is the canonical signal that a
        # concurrent writer modified the key between our WATCH and EXEC.
        watch_error_cls = importlib.import_module("redis.exceptions").WatchError

        async with self._client.pipeline(transaction=True) as pipe:
            for attempt in range(max_retries + 1):
                try:
                    await pipe.watch(key)
                    raw: str | None = await pipe.get(key)
                    current = SessionState.model_validate_json(raw) if raw is not None else None
                    new_state = mutator(current)
                    pipe.multi()
                    pipe.set(key, new_state.model_dump_json())
                    await pipe.execute()
                    return new_state
                except watch_error_cls:
                    log.info(
                        "redis.cas.watch_conflict",
                        session_id=str(session_id),
                        attempt=attempt,
                    )
                    # Loop back; pipe is reset by the next watch() call.
                    continue

        log.error(
            "redis.cas.exhausted",
            session_id=str(session_id),
            max_retries=max_retries,
        )
        raise RedisCasExhaustedError(
            f"WATCH/MULTI for session {session_id} exhausted "
            f"{max_retries + 1} attempts; concurrent writer wins."
        )

    async def delete(self, session_id: UUID) -> int:
        """Remove the session key. Returns 1 if it existed, 0 otherwise.

        Called from the agent entrypoint's `finally` block on session
        teardown. Skipping this leaves an orphan in Redis until M6's
        stale-session reaper or a manual purge — see CLAUDE.md Gotchas.
        """
        return await self._client.delete(_state_key(session_id))

    async def aclose(self) -> None:
        """Close the underlying connection pool. Idempotent."""
        await self._client.aclose()


_STORE_SINGLETON: RedisSessionStore | None = None
_STORE_LOCK = threading.Lock()


def get_redis_store(settings: Settings | None = None) -> RedisSessionStore:
    """Return the process-wide RedisSessionStore singleton.

    Mirrors the threading.Lock + double-check pattern used by
    `audio/stt._load_model` and `tts/kokoro._load_engine` because the
    asyncio default thread-pool executor can race the first
    `from_url` call from two coroutines.
    """
    global _STORE_SINGLETON
    if _STORE_SINGLETON is not None:
        return _STORE_SINGLETON
    with _STORE_LOCK:
        if _STORE_SINGLETON is not None:
            return _STORE_SINGLETON
        cfg = settings or get_settings()
        log.info("redis.store.init", url=cfg.redis_url)
        _STORE_SINGLETON = RedisSessionStore(cfg.redis_url)
        return _STORE_SINGLETON


def reset_redis_store_singleton() -> None:
    """Test-only: drop the cached singleton.

    Production code never calls this; the connection pool lives for the
    lifetime of the process. Tests use it to swap fakeredis backends
    between cases.
    """
    global _STORE_SINGLETON
    _STORE_SINGLETON = None
