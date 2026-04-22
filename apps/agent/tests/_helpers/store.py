"""In-memory `RedisSessionStore` substitute.

Exposes the same async surface (`load`, `put`, `apply`, `delete`,
`aclose`) the router/agent depend on. CAS is trivial in single-thread
asyncio — we just call the mutator on the current state and store
the result. Tests that need to exercise CAS contention should use the
real `RedisSessionStore` against fakeredis (see `test_redis_store.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from archmentor_agent.state.session_state import SessionState

StateMutator = Callable[[SessionState | None], SessionState]


class FakeSessionStore:
    """Deterministic in-memory store for router/agent tests."""

    def __init__(self) -> None:
        self._states: dict[UUID, SessionState] = {}
        self.cas_error: BaseException | None = None
        self.apply_calls: int = 0

    async def load(self, session_id: UUID) -> SessionState | None:
        return self._states.get(session_id)

    async def put(self, session_id: UUID, state: SessionState) -> None:
        self._states[session_id] = state

    async def apply(
        self,
        session_id: UUID,
        mutator: StateMutator,
        *,
        max_retries: int = 3,
    ) -> SessionState:
        self.apply_calls += 1
        if self.cas_error is not None:
            raise self.cas_error
        current = self._states.get(session_id)
        new_state = mutator(current)
        self._states[session_id] = new_state
        return new_state

    async def delete(self, session_id: UUID) -> int:
        return 1 if self._states.pop(session_id, None) is not None else 0

    async def aclose(self) -> None:
        return None


__all__ = ["FakeSessionStore"]
