"""Tests for `archmentor_agent.state.redis_store.RedisSessionStore`.

Most tests run against an in-process `fakeredis.FakeAsyncRedis`. The
concurrent-CAS test uses two clients bound to a shared `FakeServer` so
they observe each other's writes (per fakeredis README/docs — relying
on default cross-instance shared state is brittle and was the source
of fakeredis #218/#297).

Real-Redis integration coverage lives behind `@pytest.mark.integration`
and assumes `./scripts/dev.sh` has booted the docker-compose Redis. CI
skips it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import fakeredis
import pytest
from archmentor_agent.config import get_settings, reset_settings_cache
from archmentor_agent.state import (
    RedisCasExhaustedError,
    RedisSessionStore,
    SessionState,
)
from archmentor_agent.state.redis_store import _redact_redis_url
from archmentor_agent.state.session_state import (
    DesignDecision,
    InterviewPhase,
    ProblemCard,
)


def test_redact_redis_url_strips_password() -> None:
    """Userinfo (including the embedded password) must be dropped so
    ``redis.store.init`` log lines don't leak credentials from the
    URL-encoded form of the config. The returned URL still identifies
    the host so logs remain useful.
    """
    assert (
        _redact_redis_url("redis://:hunter2@redis.internal:6379/0")
        == "redis://redis.internal:6379/0"
    )
    # user-only (no password) and plain URLs round-trip.
    assert (
        _redact_redis_url("redis://admin@redis.internal:6379/0") == "redis://redis.internal:6379/0"
    )
    assert _redact_redis_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
    # Malformed input fails closed rather than leaking a half-parsed URL.
    assert _redact_redis_url("not a url") == "<invalid-url>"


def _make_state(*, decisions: list[DesignDecision] | None = None) -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="dev-test",
            version=1,
            title="Dev Test",
            statement_md="Stub problem.",
            rubric_yaml="dimensions: []",
        ),
        system_prompt_version="m2-test",
        started_at=datetime(2026, 4, 22, tzinfo=UTC),
        phase=InterviewPhase.INTRO,
        decisions=decisions or [],
    )


def _store_with_fake(server: fakeredis.FakeServer | None = None) -> RedisSessionStore:
    fake_client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return RedisSessionStore("redis://fake/0", client=fake_client)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    reset_settings_cache()


async def test_round_trip_preserves_decisions_log() -> None:
    sid = uuid4()
    store = _store_with_fake()
    state = _make_state(
        decisions=[
            DesignDecision(t_ms=1000, decision="Use Postgres", reasoning="ACID needed"),
            DesignDecision(t_ms=2000, decision="Add Redis cache", reasoning="Read-heavy"),
        ],
    )
    await store.put(sid, state)
    loaded = await store.load(sid)
    assert loaded is not None
    assert len(loaded.decisions) == 2
    assert loaded.decisions[0].decision == "Use Postgres"
    assert loaded.decisions[1].reasoning == "Read-heavy"
    assert loaded.problem.slug == "dev-test"


async def test_load_missing_key_returns_none() -> None:
    store = _store_with_fake()
    assert await store.load(uuid4()) is None


async def test_put_does_not_set_ttl() -> None:
    """No TTL on session keys — explicit cleanup discipline (CLAUDE.md)."""
    sid = uuid4()
    store = _store_with_fake()
    await store.put(sid, _make_state())
    pttl: int = await store._client.pttl(f"session:{sid}:state")
    # redis-py: -1 means "key exists but has no expiry".
    assert pttl == -1


async def test_delete_removes_key() -> None:
    sid = uuid4()
    store = _store_with_fake()
    await store.put(sid, _make_state())
    assert await store.delete(sid) == 1
    assert await store.load(sid) is None


async def test_delete_missing_key_returns_zero() -> None:
    store = _store_with_fake()
    assert await store.delete(uuid4()) == 0


async def test_apply_creates_state_when_absent() -> None:
    sid = uuid4()
    store = _store_with_fake()

    def init(current: SessionState | None) -> SessionState:
        assert current is None
        return _make_state(
            decisions=[
                DesignDecision(t_ms=0, decision="Bootstrap", reasoning="First write"),
            ]
        )

    new_state = await store.apply(sid, init)
    assert new_state.decisions[0].decision == "Bootstrap"
    loaded = await store.load(sid)
    assert loaded is not None
    assert loaded.decisions[0].decision == "Bootstrap"


async def test_apply_appends_to_decisions_log() -> None:
    sid = uuid4()
    store = _store_with_fake()
    await store.put(
        sid, _make_state(decisions=[DesignDecision(t_ms=0, decision="Initial", reasoning="A")])
    )

    def append(current: SessionState | None) -> SessionState:
        assert current is not None
        current.decisions.append(DesignDecision(t_ms=1000, decision="Second", reasoning="B"))
        return current

    new_state = await store.apply(sid, append)
    assert [d.decision for d in new_state.decisions] == ["Initial", "Second"]


async def test_concurrent_apply_via_shared_server_no_lost_updates() -> None:
    """Two FakeAsyncRedis clients on a shared FakeServer race on apply.

    Both writers append a distinct decision; the WATCH/MULTI loop must
    see the other's update on retry and preserve both. Without the CAS
    retry, one writer would overwrite the other's append.
    """
    sid = uuid4()
    server = fakeredis.FakeServer()
    store_a = _store_with_fake(server=server)
    store_b = _store_with_fake(server=server)
    await store_a.put(sid, _make_state())

    def append_factory(decision_text: str) -> Any:
        def _mut(current: SessionState | None) -> SessionState:
            assert current is not None
            current.decisions.append(
                DesignDecision(
                    t_ms=len(current.decisions),
                    decision=decision_text,
                    reasoning="concurrent",
                )
            )
            return current

        return _mut

    # asyncio.gather schedules both coroutines on the same loop. The CAS
    # WATCH on one will detect the other's commit and trigger a retry.
    await asyncio.gather(
        store_a.apply(sid, append_factory("from_a")),
        store_b.apply(sid, append_factory("from_b")),
    )

    final = await store_a.load(sid)
    assert final is not None
    decisions = {d.decision for d in final.decisions}
    assert decisions == {"from_a", "from_b"}, f"Expected both updates preserved, got {decisions}"


async def test_apply_raises_when_cas_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the WatchError path on every retry by mutating the key
    between WATCH and EXEC. After `max_retries+1` attempts, raise
    `RedisCasExhaustedError` — never silently lose the update."""
    sid = uuid4()
    server = fakeredis.FakeServer()
    store = _store_with_fake(server=server)
    interferer = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    await store.put(sid, _make_state())

    interfere_count = 0
    sync = fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    def mut(current: SessionState | None) -> SessionState:
        # The mutator runs BETWEEN watch+get and multi+set. A synchronous
        # write from a different client (sharing the FakeServer)
        # invalidates the WATCH, so EXEC raises WatchError every retry.
        nonlocal interfere_count
        interfere_count += 1
        assert current is not None
        sync.set(f"session:{sid}:state", current.model_dump_json())
        return current

    with pytest.raises(RedisCasExhaustedError):
        await store.apply(sid, mut, max_retries=2)
    # Mutator was called once per attempt: max_retries + initial = 3.
    assert interfere_count == 3
    await interferer.aclose()


async def test_get_redis_store_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_redis_store` returns the same instance across calls."""
    from archmentor_agent.state.redis_store import (
        get_redis_store,
        reset_redis_store_singleton,
    )

    reset_redis_store_singleton()
    settings = get_settings()
    a = get_redis_store(settings)
    b = get_redis_store(settings)
    assert a is b
    reset_redis_store_singleton()


@pytest.mark.integration
async def test_real_redis_round_trip() -> None:
    """End-to-end CAS path against the docker-compose Redis.

    Skipped unless the integration marker is selected. Guards against
    fakeredis WATCH/MULTI fidelity drift vs real Redis 7.4 (the M2 plan
    risks-table calls this out).
    """
    import importlib

    redis_async = importlib.import_module("redis.asyncio")
    settings = get_settings()
    real_client = redis_async.from_url(settings.redis_url, decode_responses=True)
    store = RedisSessionStore(settings.redis_url, client=real_client)

    sid = uuid4()
    try:

        def init(_: SessionState | None) -> SessionState:
            return _make_state(
                decisions=[
                    DesignDecision(t_ms=0, decision="real-redis", reasoning="check"),
                ]
            )

        await store.apply(sid, init)
        loaded = await store.load(sid)
        assert loaded is not None
        assert loaded.decisions[0].decision == "real-redis"
        # No-TTL invariant must hold against real Redis too.
        pttl: int = await real_client.pttl(f"session:{sid}:state")
        assert pttl == -1
    finally:
        await store.delete(sid)
        await real_client.aclose()
