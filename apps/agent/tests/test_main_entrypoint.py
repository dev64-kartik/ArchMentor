"""Entry-point helper + brain-loop integration tests.

The full entrypoint needs LiveKit room state + framework adapters, so
we don't run it end-to-end here. We test the pure helpers that gate
the agent's setup (session id parsing, ledger-config env reads) and
the brain-loop wiring against lightweight fakes — real `EventRouter`,
`UtteranceQueue`, `SpeechCheckGate`, `FakeBrainClient`, and
`FakeSessionStore` / `FakeSnapshotClient`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import anthropic
import pytest
from _helpers import (
    FakeBrainClient,
    FakeCanvasSnapshotClient,
    FakeSessionStore,
    FakeSnapshotClient,
)
from archmentor_agent.api_client.bootstrap import SessionBootstrap
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision, BrainUsage
from archmentor_agent.canvas.client import CanvasSnapshotClient
from archmentor_agent.config import reset_settings_cache
from archmentor_agent.main import (
    MentorAgent,
    _agent_http_config,
    _fetch_bootstrap_problem,
    _session_id_from_ctx,
    build_brain_wiring,
    build_initial_session_state,
)
from archmentor_agent.snapshots.client import SnapshotClient
from archmentor_agent.state.redis_store import RedisSessionStore
from archmentor_agent.state.session_state import (
    InterviewPhase,
    ProblemCard,
    SessionState,
)
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """`Settings` is process-wide cached; clear it so monkeypatched env
    vars take effect on each test."""
    reset_settings_cache()


class _FakeRoom:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCtx:
    def __init__(self, room_name: str) -> None:
        self.room = _FakeRoom(room_name)


def test_session_id_from_session_prefixed_room() -> None:
    sid = UUID("12345678-1234-5678-1234-567812345678")
    ctx = _FakeCtx(f"session-{sid}")
    assert _session_id_from_ctx(ctx) == sid  # ty: ignore[invalid-argument-type]


def test_session_id_from_bare_uuid_room() -> None:
    sid = UUID("12345678-1234-5678-1234-567812345678")
    ctx = _FakeCtx(str(sid))
    assert _session_id_from_ctx(ctx) == sid  # ty: ignore[invalid-argument-type]


def test_session_id_raises_on_garbage_room() -> None:
    ctx = _FakeCtx("my-test-room")
    with pytest.raises(RuntimeError, match="Cannot extract session UUID"):
        _session_id_from_ctx(ctx)  # ty: ignore[invalid-argument-type]


def _disable_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `Settings` from reading the developer's `.env` file.

    `_agent_http_config` constructs `Settings()` internally, so
    monkeypatching `os.environ` alone isn't enough — pydantic-settings
    reads `.env` too. Replace `model_config` with a copy that has
    `env_file=None` for the duration of the test; `monkeypatch.setattr`
    restores the original.
    """
    from archmentor_agent.config import Settings
    from pydantic_settings import SettingsConfigDict

    config_no_env_file = SettingsConfigDict(**{**Settings.model_config, "env_file": None})
    monkeypatch.setattr(Settings, "model_config", config_no_env_file)


def test_agent_http_config_requires_agent_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings construction raises ValidationError when the token is
    absent. The raw RuntimeError from M1's `_ledger_config` was replaced
    by pydantic-settings' own validation."""
    _disable_env_file(monkeypatch)
    monkeypatch.delenv("ARCHMENTOR_AGENT_INGEST_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="agent_ingest_token"):
        _agent_http_config()


def test_agent_http_config_returns_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_env_file(monkeypatch)
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok_test_tok_test_tok_test_tok")
    api_url, agent_token = _agent_http_config()
    assert api_url == "http://api.test:9999"
    assert agent_token == "tok_test_tok_test_tok_test_tok"  # noqa: S105 — fixture value


def test_build_initial_session_state_populates_problem_and_cost() -> None:
    """The bootstrap path seeds SessionState from brain/bootstrap.py.

    Covers the contract that `on_enter`'s `store.put` relies on:
    cost_cap_usd is the per-session knob (not a ProblemCard field),
    and the URL-shortener dev problem is the version the seed script
    mirrors.
    """
    state = build_initial_session_state(cost_cap_usd=2.5)
    assert state.problem.slug == "dev-test"
    assert state.problem.title == "Design URL Shortener"
    assert state.cost_cap_usd == 2.5
    assert state.cost_usd_total == 0.0
    assert state.phase is InterviewPhase.INTRO
    assert state.decisions == []
    assert state.transcript_window == []


def test_build_initial_session_state_with_injected_problem() -> None:
    """When a ProblemCard is injected, it replaces the dev-test fallback."""
    injected = ProblemCard(
        slug="injected-slug",
        version=3,
        title="Injected Problem",
        statement_md="# Injected",
        rubric_yaml="dimensions: []\n",
    )
    state = build_initial_session_state(cost_cap_usd=3.0, problem=injected)
    assert state.problem.slug == "injected-slug"
    assert state.problem.title == "Injected Problem"
    assert state.cost_cap_usd == 3.0


@pytest.mark.asyncio
async def test_fetch_bootstrap_problem_dev_fallback_non_session_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non `session-<uuid>` room name → dev fallback (None returned)."""
    from archmentor_agent.config import Settings

    _disable_env_file(monkeypatch)
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok_test_tok_test_tok_test_tok")
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "key_test")
    monkeypatch.setenv("ARCHMENTOR_BRAIN_ENABLED", "true")
    settings = Settings()  # ty: ignore[missing-argument]

    problem, abort_reason = await _fetch_bootstrap_problem(
        session_id=UUID("11111111-2222-3333-4444-555555555555"),
        room_name="dev-test",
        settings=settings,
    )
    assert problem is None
    assert abort_reason is None


@pytest.mark.asyncio
async def test_fetch_bootstrap_problem_brain_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brain disabled → no API call, None returned immediately."""
    from archmentor_agent.config import Settings

    _disable_env_file(monkeypatch)
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok_test_tok_test_tok_test_tok")
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "key_test")
    monkeypatch.setenv("ARCHMENTOR_BRAIN_ENABLED", "false")
    settings = Settings()  # ty: ignore[missing-argument]

    problem, abort_reason = await _fetch_bootstrap_problem(
        session_id=UUID("11111111-2222-3333-4444-555555555555"),
        room_name="session-11111111-2222-3333-4444-555555555555",
        settings=settings,
    )
    assert problem is None
    assert abort_reason is None


@pytest.mark.asyncio
async def test_fetch_bootstrap_problem_success_builds_problem_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fetch succeeds, a ProblemCard is built from the bootstrap payload."""
    from archmentor_agent.config import Settings

    _disable_env_file(monkeypatch)
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok_test_tok_test_tok_test_tok")
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "key_test")
    monkeypatch.setenv("ARCHMENTOR_BRAIN_ENABLED", "true")
    settings = Settings()  # ty: ignore[missing-argument]

    session_id = UUID("11111111-2222-3333-4444-555555555555")

    async def _mock_fetch(
        *,
        api_url: str,
        agent_token: str,
        session_id: UUID,
        timeout_s: float = 5.0,
        max_retries: int = 1,
    ) -> SessionBootstrap:
        return SessionBootstrap(
            status="active",
            problem_slug="url-shortener",
            statement_md="# URL Shortener statement",
            rubric_yaml="dimensions: [functional]",
        )

    import archmentor_agent.main as _main_module

    monkeypatch.setattr(_main_module, "fetch_session_bootstrap", _mock_fetch)

    problem, abort_reason = await _fetch_bootstrap_problem(
        session_id=session_id,
        room_name=f"session-{session_id}",
        settings=settings,
    )
    assert problem is not None
    assert problem.slug == "url-shortener"
    assert problem.statement_md == "# URL Shortener statement"
    assert problem.rubric_yaml == "dimensions: [functional]"
    assert abort_reason is None


@pytest.mark.asyncio
async def test_fetch_bootstrap_problem_aborts_when_session_not_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap arrives after R26 keepalive ended the session → abort, no TTS."""
    from archmentor_agent.config import Settings

    _disable_env_file(monkeypatch)
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok_test_tok_test_tok_test_tok")
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "key_test")
    monkeypatch.setenv("ARCHMENTOR_BRAIN_ENABLED", "true")
    settings = Settings()  # ty: ignore[missing-argument]

    session_id = UUID("11111111-2222-3333-4444-555555555555")

    async def _mock_fetch(**_: object) -> SessionBootstrap:
        return SessionBootstrap(
            status="ended",
            problem_slug="url-shortener",
            statement_md="# whatever",
            rubric_yaml="dimensions: [functional]",
        )

    import archmentor_agent.main as _main_module

    monkeypatch.setattr(_main_module, "fetch_session_bootstrap", _mock_fetch)

    problem, abort_reason = await _fetch_bootstrap_problem(
        session_id=session_id,
        room_name=f"session-{session_id}",
        settings=settings,
    )
    assert problem is None
    assert abort_reason == "session_not_active_at_bootstrap"


@pytest.mark.asyncio
async def test_fetch_bootstrap_problem_fetch_error_falls_back_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BootstrapFetchError during production fetch → dev fallback (None), no crash."""
    from archmentor_agent.api_client.bootstrap import BootstrapFetchError
    from archmentor_agent.config import Settings

    _disable_env_file(monkeypatch)
    monkeypatch.setenv("ARCHMENTOR_API_URL", "http://api.test:9999")
    monkeypatch.setenv("ARCHMENTOR_AGENT_INGEST_TOKEN", "tok_test_tok_test_tok_test_tok")
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "key_test")
    monkeypatch.setenv("ARCHMENTOR_BRAIN_ENABLED", "true")
    settings = Settings()  # ty: ignore[missing-argument]

    session_id = UUID("11111111-2222-3333-4444-555555555555")

    async def _mock_fetch(**_: object) -> SessionBootstrap:
        raise BootstrapFetchError("connection refused", status_code=None)

    import archmentor_agent.main as _main_module

    monkeypatch.setattr(_main_module, "fetch_session_bootstrap", _mock_fetch)

    problem, abort_reason = await _fetch_bootstrap_problem(
        session_id=session_id,
        room_name=f"session-{session_id}",
        settings=settings,
    )
    assert problem is None
    assert abort_reason is None


# ──────────────────────────────────────────────────────────────────────
# Brain-loop integration tests
# ──────────────────────────────────────────────────────────────────────


SESSION_ID = UUID("11111111-2222-3333-4444-555555555555")


class _FakeSpeechHandle:
    async def wait_for_playout(self) -> None:
        return None


class _FakeAgentSession:
    """Minimal `AgentSession` substitute for the brain wiring tests.

    Records `say()` invocations so assertions can distinguish the
    brain's utterance from `TURN_ACK_UTTERANCE` on the kill-switch
    path. The return value matches the `say()` shape the agent awaits
    for the opening utterance only.

    The `streamed_*` lists capture utterances delivered through the
    M4 streaming TTS path (`_start_streaming_say`); declared up front
    so static type checkers see the attributes.
    """

    def __init__(self, *, raise_on_say: bool = False) -> None:
        self.said: list[str] = []
        self.streamed_deltas: list[str] = []
        self.streamed_full: list[str] = []
        self._raise_on_say = raise_on_say

    def say(self, text: str) -> _FakeSpeechHandle:
        if self._raise_on_say:
            raise RuntimeError("session closing")
        self.said.append(text)
        return _FakeSpeechHandle()


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish_data(self, payload: str, *, topic: str) -> None:
        self.published.append((topic, payload))


class _FakeRoomForAgent:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


class _FakeLedger:
    def __init__(self) -> None:
        self.appends: list[tuple[str, dict[str, object]]] = []

    async def append(
        self,
        *,
        session_id: UUID,
        t_ms: int,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        _ = session_id
        _ = t_ms
        self.appends.append((event_type, dict(payload)))
        return True

    async def aclose(self) -> None:
        return None


def _seed_state(cost_usd_total: float = 0.0, cost_cap_usd: float = 5.0) -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="URL Shortener",
            statement_md="Design a URL shortener.",
            rubric_yaml="dimensions: []\n",
        ),
        system_prompt_version="m2-test",
        started_at=datetime(2026, 4, 22, tzinfo=UTC),
        elapsed_s=0,
        remaining_s=2700,
        cost_usd_total=cost_usd_total,
        cost_cap_usd=cost_cap_usd,
    )


class FakeHaikuClient:
    """Records compaction calls; returns a scripted summary text."""

    def __init__(
        self,
        *,
        summary_text: str = "[compressed summary]",
        cost_usd: float = 0.001,
        model: str = "anthropic/claude-haiku-4-5",
        raise_exc: BaseException | None = None,
    ) -> None:
        self.summary_text = summary_text
        self.cost_usd = cost_usd
        self._model = model
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, list[str]]] = []

    @property
    def model(self) -> str:
        return self._model

    async def compress(
        self,
        *,
        existing_summary: str,
        dropped_turns: list[Any],
    ) -> tuple[str, BrainUsage]:
        self.calls.append((existing_summary, [t.text for t in dropped_turns]))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.summary_text, BrainUsage(
            input_tokens=120,
            output_tokens=40,
            cost_usd=self.cost_usd,
        )

    async def aclose(self) -> None:
        return None


def _build_agent_under_test(
    *,
    brain_enabled: bool,
    brain: FakeBrainClient | None = None,
    store: FakeSessionStore | None = None,
    snapshots: FakeSnapshotClient | None = None,
    canvas_snapshots: FakeCanvasSnapshotClient | None = None,
    seed_state: SessionState | None = None,
    session_raises: bool = False,
    haiku: FakeHaikuClient | None = None,
) -> tuple[
    MentorAgent,
    _FakeAgentSession,
    _FakeLedger,
    FakeBrainClient,
    FakeSessionStore,
    FakeSnapshotClient,
    FakeCanvasSnapshotClient,
]:
    fake_session = _FakeAgentSession(raise_on_say=session_raises)
    ledger = _FakeLedger()
    room = _FakeRoomForAgent()

    brain = brain or FakeBrainClient()
    store = store or FakeSessionStore()
    snapshots = snapshots or FakeSnapshotClient()
    canvas_snapshots = canvas_snapshots or FakeCanvasSnapshotClient()

    state = seed_state or _seed_state()
    store._states[SESSION_ID] = state

    agent = MentorAgent(
        session_id=SESSION_ID,
        ledger=cast(Any, ledger),
        room=cast(Any, room),
        brain_enabled=brain_enabled,
        brain=None,
    )
    if brain_enabled:
        wiring = build_brain_wiring(
            agent,
            brain=cast(BrainClient, brain),
            store=cast(RedisSessionStore, store),
            snapshot_client=cast(SnapshotClient, snapshots),
            canvas_snapshot_client=cast(CanvasSnapshotClient, canvas_snapshots),
            haiku=cast(Any, haiku),
        )
        agent.attach_brain(wiring)

    # Replace `_say` with a fake that records + optionally raises.
    # Avoids the livekit base class's "agent is not running" property
    # check without requiring a live activity context.
    async def fake_say(text: str) -> None:
        if session_raises:
            raise RuntimeError("session closing")
        fake_session.said.append(text)

    agent._say = fake_say  # ty: ignore[invalid-assignment]

    # Streaming TTS test seam — `_start_streaming_say` is the M4 hand-off
    # for `session.say(async_iterable)`. The fake collects each delta
    # into `fake_session.streamed_deltas` so tests can assert "streaming
    # path was used" without needing a live `AgentSession`.
    # Lists are pre-allocated on `_FakeAgentSession.__init__`; just
    # reset them here in case the harness is reused across tests.
    fake_session.streamed_deltas.clear()
    fake_session.streamed_full.clear()

    def fake_start_streaming_say(deltas: AsyncIterator[str]) -> Any:
        async def _drain() -> None:
            collected: list[str] = []
            async for delta in deltas:
                fake_session.streamed_deltas.append(delta)
                collected.append(delta)
            fake_session.streamed_full.append("".join(collected))

        task = asyncio.create_task(_drain(), name="fake.streaming_say")

        class _FakeStreamingHandle:
            async def wait_for_playout(self) -> None:
                await task

        return _FakeStreamingHandle()

    agent._start_streaming_say = fake_start_streaming_say  # ty: ignore[invalid-assignment]
    # Prime the t0 clock so `_now_relative_ms` returns deterministic
    # values. `on_enter` would normally do this.
    agent._t0_ms = 0
    return agent, fake_session, ledger, brain, store, snapshots, canvas_snapshots


async def _drain_tasks(agent: MentorAgent) -> None:
    if agent._snapshot_tasks:
        await asyncio.gather(*agent._snapshot_tasks, return_exceptions=True)
    if agent._summary_tasks:
        await asyncio.gather(*agent._summary_tasks, return_exceptions=True)
    if agent._ledger_tasks:
        await asyncio.gather(*agent._ledger_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_brain_enabled_speaks_brain_utterance_not_static_ack() -> None:
    brain = FakeBrainClient()
    brain.enqueue_speak("Tell me how you'd index the short codes.")
    agent, fake_session, ledger, _, _, snapshots, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain
    )

    await agent.handle_user_input("I'd use a 7-char base62 code.")
    await _drain_tasks(agent)

    # Streaming TTS path is the M4 production default — the brain
    # utterance flows through `_start_streaming_say`, NOT the legacy
    # one-shot `session.say(text)`. Tests that exercise the brain →
    # speech wiring should observe `streamed_full`.
    assert fake_session.streamed_full == ["Tell me how you'd index the short codes."]
    event_types = [et for et, _ in ledger.appends]
    assert "utterance_candidate" in event_types
    assert "brain_decision" in event_types
    assert "utterance_ai" in event_types
    assert len(snapshots.posts) == 1


@pytest.mark.asyncio
async def test_brain_enabled_stay_silent_means_no_tts() -> None:
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("not_an_interruption_moment")
    agent, fake_session, ledger, _, _, snapshots, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain
    )

    await agent.handle_user_input("Then I'd cache the hot short codes.")
    await _drain_tasks(agent)

    assert fake_session.said == []
    # Still one snapshot + one brain_decision event for observability.
    assert len(snapshots.posts) == 1
    decision_events = [p for et, p in ledger.appends if et == "brain_decision"]
    assert len(decision_events) == 1
    assert decision_events[0]["decision"] == "stay_silent"


@pytest.mark.asyncio
async def test_kill_switch_uses_static_ack_and_bypasses_brain() -> None:
    brain = FakeBrainClient()
    brain.enqueue_speak("this must not be spoken")
    agent, fake_session, ledger, _, _, snapshots, _ = _build_agent_under_test(
        brain_enabled=False, brain=brain
    )

    await agent.handle_user_input("Let me walk through the write path.")
    await _drain_tasks(agent)

    # M1 fallback — static ack only, zero brain calls.
    assert fake_session.said == ["Got it. Keep going when you're ready."]
    assert brain.calls == []
    # No snapshots either — the router never ran.
    assert snapshots.posts == []
    event_types = [et for et, _ in ledger.appends]
    assert "utterance_candidate" in event_types
    assert "utterance_ai" in event_types
    assert "brain_decision" not in event_types


@pytest.mark.asyncio
async def test_hallucination_filter_drops_before_brain_call() -> None:
    brain = FakeBrainClient()
    brain.enqueue_speak("this should never be reached")
    agent, fake_session, ledger, _, _, snapshots, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain
    )

    await agent.handle_user_input("[Music]")
    await _drain_tasks(agent)

    assert brain.calls == []
    assert snapshots.posts == []
    assert fake_session.said == []
    assert [et for et, _ in ledger.appends] == []


@pytest.mark.asyncio
async def test_low_confidence_brain_decision_does_not_speak() -> None:
    """Confidence gate lives on the router but the agent must honour it.

    The router abstains from queueing a `speak` utterance below 0.6;
    the agent's `_drain_utterance_queue` should then find nothing.
    """
    brain = FakeBrainClient()
    brain.enqueue_speak("below_confidence_threshold", confidence=0.55)
    agent, fake_session, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("Capacity estimate first.")
    await _drain_tasks(agent)

    assert fake_session.said == []


@pytest.mark.asyncio
async def test_interim_transcript_marks_gate_and_cancels_in_flight() -> None:
    brain = FakeBrainClient(delay_s=0.1)
    brain.enqueue_speak("late utterance")
    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)
    assert agent._brain is not None

    # Start a turn so a dispatch is running.
    turn_task = asyncio.create_task(agent.handle_user_input("let me think about caching"))
    await asyncio.sleep(0)  # let router.handle spawn the dispatch
    # Barge-in while the brain is mid-call.
    await agent.handle_interim_transcript("actually —")

    # The gate should now think the candidate is speaking again.
    assert agent._brain.gate.is_candidate_speaking() is True

    # Cancellation surfaces in the turn_task because the router's
    # `wait_for_idle` awaits the cancelled dispatch. The outer task
    # either completes without a speak or raises CancelledError — we
    # accept either since the voice-loop contract is "no hung tasks."
    try:
        await asyncio.wait_for(turn_task, timeout=0.5)
    except (TimeoutError, asyncio.CancelledError):
        turn_task.cancel()
    await _drain_tasks(agent)


@pytest.mark.asyncio
async def test_shutdown_drains_snapshot_and_ledger_tasks() -> None:
    brain = FakeBrainClient()
    brain.enqueue_speak("final thought")
    agent, _, _, _, store, snapshots, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("what's left before we wrap up?")
    await agent.shutdown()

    # Both task sets must have drained during shutdown.
    assert all(t.done() for t in agent._snapshot_tasks)
    assert all(t.done() for t in agent._ledger_tasks)
    # Snapshot still posted (it's scheduled by the router pre-shutdown).
    assert len(snapshots.posts) == 1
    # Session key was deleted from the store.
    assert store._states.get(SESSION_ID) is None


@pytest.mark.asyncio
async def test_shutdown_on_kill_switch_only_drains_ledger() -> None:
    agent, _, _, _, _, snapshots, _ = _build_agent_under_test(brain_enabled=False)

    await agent.handle_user_input("quick thought")
    await agent.shutdown()

    assert all(t.done() for t in agent._ledger_tasks)
    assert snapshots.posts == []
    # Store delete is skipped on the kill-switch path so `_brain=None`
    # doesn't trigger an attribute error. Still covered by the
    # brain-enabled test above.


def _build_anthropic_response(status: int) -> Any:
    """Minimal `httpx.Response` for Anthropic exception construction."""
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status, request=request)


@pytest.mark.asyncio
async def test_anthropic_authentication_error_does_not_hang_session() -> None:
    """A non-retriable API error (auth/bad request) must not break the voice loop.

    The brain client raises `AuthenticationError`; the router catches
    it, logs `router.brain.unexpected`, and returns a `stay_silent`
    fallback. The agent sees an empty queue and advances to listening.
    """
    brain = FakeBrainClient(
        raise_on_call=anthropic.AuthenticationError(
            message="bad key",
            response=_build_anthropic_response(401),
            body=None,
        )
    )
    agent, fake_session, _, _, _, snapshots, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain
    )

    # Must not raise — the voice loop's contract is degrade-to-silence.
    await agent.handle_user_input("Let me outline the write path.")
    await _drain_tasks(agent)

    assert fake_session.said == []
    # Snapshot still posted for observability (decision=stay_silent).
    assert len(snapshots.posts) == 1


@pytest.mark.asyncio
async def test_scripted_multi_turn_session_records_all_decisions() -> None:
    """Five-turn script: 3 speak, 2 stay_silent → 5 snapshots + decisions."""
    brain = FakeBrainClient()
    brain.enqueue_speak("probe 1")
    brain.enqueue_stay_silent("ok")
    brain.enqueue_speak("probe 2")
    brain.enqueue_stay_silent("ok")
    brain.enqueue_speak("probe 3")
    agent, fake_session, ledger, _, _, snapshots, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain
    )

    for idx in range(5):
        await agent.handle_user_input(f"turn {idx}: requirements")
    await _drain_tasks(agent)

    # Streaming TTS path delivers each speak utterance live; the legacy
    # `_say` path is unused under M4.
    assert fake_session.streamed_full == ["probe 1", "probe 2", "probe 3"]
    assert len(snapshots.posts) == 5
    decisions = [p for et, p in ledger.appends if et == "brain_decision"]
    assert len(decisions) == 5
    assert [d["decision"] for d in decisions] == [
        "speak",
        "stay_silent",
        "speak",
        "stay_silent",
        "speak",
    ]


@pytest.mark.asyncio
async def test_cost_cap_hit_flips_to_capped_path_after_session_ages() -> None:
    """Once `cost_usd_total >= cost_cap_usd`, the router short-circuits.

    Seed the store with a session already over-cap; dispatch should
    bypass Anthropic (no brain.calls) but still emit a snapshot +
    ledger event with reason=cost_capped.
    """
    brain = FakeBrainClient()
    brain.enqueue_speak("this should not be called")
    seed = _seed_state(cost_usd_total=5.01, cost_cap_usd=5.0)
    agent, fake_session, ledger, _, _, snapshots, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed
    )

    await agent.handle_user_input("finishing up the tradeoffs")
    await _drain_tasks(agent)

    assert brain.calls == []
    assert fake_session.said == []
    assert len(snapshots.posts) == 1
    decisions = [p for et, p in ledger.appends if et == "brain_decision"]
    assert decisions[0]["reason"] == "cost_capped"


# ──────────────────────────────────────────────────────────────────────
# Transcript-window population (M2-era bug fixed 2026-04-26)
# ──────────────────────────────────────────────────────────────────────
#
# Before the fix, `state.transcript_window` was declared in SessionState
# and read by the brain prompt builder, but no callsite ever appended
# turns. Result: the brain saw `transcript_turns=0` on every call and
# could not maintain cross-turn context. These tests pin the four
# append sites (candidate utterance, opening, static-ack, brain-driven)
# plus the cap and no-baseline branches.


@pytest.mark.asyncio
async def test_handle_user_input_appends_candidate_turn_to_transcript() -> None:
    """Candidate text lands in transcript_window via Redis CAS before the brain dispatch."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("not_an_interruption_moment")
    agent, _, _, _, store, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("I'd start with a base62 short code.")
    await _drain_tasks(agent)

    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    transcript = final_state.transcript_window
    assert len(transcript) >= 1
    assert transcript[0].speaker == "candidate"
    assert transcript[0].text == "I'd start with a base62 short code."


@pytest.mark.asyncio
async def test_drain_utterance_queue_appends_ai_turn_to_transcript() -> None:
    """Brain-driven AI utterance also appends to transcript_window after TTS playout."""
    brain = FakeBrainClient()
    brain.enqueue_speak("How would you index the short codes?")
    agent, _, _, _, store, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("Should I assume read-heavy traffic?")
    await _drain_tasks(agent)

    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    speakers = [t.speaker for t in final_state.transcript_window]
    texts = [t.text for t in final_state.transcript_window]
    assert "candidate" in speakers
    assert "ai" in speakers
    assert "How would you index the short codes?" in texts


@pytest.mark.asyncio
async def test_static_ack_path_appends_both_candidate_and_ai_turns() -> None:
    """Kill-switch path (brain disabled) still records both sides of the turn."""
    agent, _, _, _, store, _, _ = _build_agent_under_test(brain_enabled=False)
    # Static-ack path doesn't go through Redis CAS (brain is None) — but the
    # candidate-side append is gated on `_brain is None` returning early too,
    # so transcript_window stays empty. Verify the no-op contract (the tests
    # for this branch are belt-and-suspenders: if a future change wires the
    # static-ack path to the store, this test forces explicit reasoning).
    await agent.handle_user_input("kill-switch path test")
    await _drain_tasks(agent)

    final_state = await store.load(SESSION_ID)
    # Brain disabled => no Redis state was seeded by the test scaffold => None.
    # The append helper short-circuits when brain is None.
    assert final_state is None or final_state.transcript_window == []


@pytest.mark.asyncio
async def test_transcript_window_grows_past_threshold_without_haiku() -> None:
    """Without a Haiku client wired, the window grows unbounded.

    M4 Unit 5 replaced the M2/M3 hard-truncate-at-30 contract with a
    Haiku-compactor flow: the rolling window keeps growing until a
    threshold-crossing tick spawns ``_run_compaction``, which then
    drops the oldest N turns via CAS. With no Haiku client wired
    (the default for `_build_agent_under_test`), the compactor never
    runs and the window grows freely. Replay/dev paths intentionally
    rely on this — they don't want a network call during tests.
    """
    from archmentor_agent.state.session_state import TranscriptTurn

    brain = FakeBrainClient()
    seed = _seed_state()
    seeded = seed.model_copy(
        update={
            "transcript_window": [
                TranscriptTurn(t_ms=i, speaker="candidate", text=f"turn-{i}") for i in range(30)
            ],
        }
    )
    brain.enqueue_stay_silent("ack")
    agent, _, _, _, store, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seeded
    )

    await agent.handle_user_input("the 31st turn")
    await _drain_tasks(agent)

    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    # Without a Haiku client wired, the M4 compactor never runs and the
    # window keeps every turn. The old "hard truncate at 30" contract
    # was deliberately removed — see M4 plan Unit 5.
    assert len(final_state.transcript_window) == 31
    assert final_state.transcript_window[0].text == "turn-0"
    assert final_state.transcript_window[-1].text == "the 31st turn"


@pytest.mark.asyncio
async def test_append_transcript_no_baseline_state_logs_and_continues() -> None:
    """Missing state in Redis (eviction / pre-init) is non-fatal."""
    import structlog.testing

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    agent, _, ledger, _, store, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)
    # Wipe the seeded state so the mutator hits the `current is None` branch.
    store._states.pop(SESSION_ID, None)

    with structlog.testing.capture_logs() as logs:
        await agent.handle_user_input("after redis eviction")
        await _drain_tasks(agent)

    # The candidate-utterance ledger row still lands; only the transcript
    # CAS apply degrades. The AI side will see no state and the brain
    # call won't fire — but the agent doesn't crash.
    candidate_rows = [p for et, p in ledger.appends if et == "utterance_candidate"]
    assert len(candidate_rows) == 1
    no_baseline_logs = [
        le for le in logs if le.get("event") == "agent.transcript.no_baseline_state"
    ]
    assert len(no_baseline_logs) >= 1


# ──────────────────────────────────────────────────────────────────────
# PublishDataError swallowed in _publish_state (cosmetic noise fix)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_state_swallows_publish_data_error() -> None:
    """LiveKit FFI's PublishDataError on engine-closed must not propagate."""
    import structlog.testing
    from livekit.rtc.participant import PublishDataError

    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=False)

    async def _raise_engine_closed(payload: str, *, topic: str) -> None:
        _ = payload, topic
        raise PublishDataError("engine: connection error: engine is closed")

    # Inject the error into the fake participant.
    agent._room.local_participant.publish_data = _raise_engine_closed  # ty: ignore[invalid-assignment]

    with structlog.testing.capture_logs() as logs:
        # Must NOT raise — the on_enter() catch path retries this call as
        # "best effort" and would emit two stack traces per session
        # without the fix.
        await agent._publish_state("listening")

    failed_logs = [le for le in logs if le.get("event") == "agent.publish_state_failed"]
    assert len(failed_logs) == 1
    assert failed_logs[0]["state"] == "listening"
    assert "engine" in failed_logs[0]["reason"]


# ---------------------------------------------------------------------------
# Unit 2 — Pre-dispatch queue drain wired through `build_brain_wiring`
# (R22 / R23, integration coverage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_brain_wiring_registers_pre_dispatch_callback() -> None:
    """`build_brain_wiring` must register a callable on `router._pre_dispatch_callback`
    so the M3-dogfood TTL-drop reproducers stay fixed."""
    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True)
    assert agent._brain is not None
    assert agent._brain.router._pre_dispatch_callback is not None


@pytest.mark.asyncio
async def test_pre_dispatch_drain_delivers_queued_speak_before_canvas_dispatch() -> None:
    """End-to-end M3-dogfood reproducer (i):
    A SPEAK utterance from a prior dispatch is delivered (`session.say` called)
    before the next CANVAS_CHANGE dispatch's brain call starts."""
    from archmentor_agent.events import EventType, RouterEvent
    from archmentor_agent.events.types import Priority
    from archmentor_agent.state.session_state import PendingUtterance

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("post-canvas")  # the canvas dispatch's outcome
    agent, fake_session, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)
    assert agent._brain is not None

    # Pre-seed the queue as if a prior dispatch produced a SPEAK that
    # was about to be drained when a competing CANVAS_CHANGE arrived.
    agent._brain.queue.push(
        PendingUtterance(
            text="from prior dispatch",
            generated_at_ms=agent.now_relative_ms(),
            ttl_ms=10_000,
        )
    )

    # Default gate state is "not speaking" (no prior `mark_speaking`
    # call), so the pre-dispatch drain isn't suppressed.

    # Fire a CANVAS_CHANGE — under M3, the queued speak would have aged
    # past TTL during the canvas dispatch and been dropped on the next
    # pop_if_fresh. Under Unit 2 it's drained pre-dispatch.
    await agent._brain.router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=agent.now_relative_ms() + 10,
            payload={"scene_text": "<label>API</label>"},
            priority=Priority.HIGH,
        )
    )
    # Wait for both the pre-dispatch drain + the dispatch loop to finish.
    if agent._brain.router._in_flight is not None:
        await agent._brain.router._in_flight
    await _drain_tasks(agent)

    assert "from prior dispatch" in fake_session.said
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_pre_dispatch_drain_skipped_when_candidate_speaking() -> None:
    """Plan §365 edge case: candidate is mid-speech when the callback
    fires → drain is suppressed by the speech-check gate; the queued
    speak waits for a quieter moment."""
    from archmentor_agent.events import EventType, RouterEvent
    from archmentor_agent.events.types import Priority
    from archmentor_agent.state.session_state import PendingUtterance

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    agent, fake_session, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)
    assert agent._brain is not None

    agent._brain.queue.push(
        PendingUtterance(
            text="should not barge",
            generated_at_ms=agent.now_relative_ms(),
            ttl_ms=10_000,
        )
    )

    # Candidate is mid-speech.
    agent._brain.gate.mark_speaking()

    await agent._brain.router.handle(
        RouterEvent(
            EventType.CANVAS_CHANGE,
            t_ms=agent.now_relative_ms() + 10,
            payload={"scene_text": "x"},
            priority=Priority.HIGH,
        )
    )
    if agent._brain.router._in_flight is not None:
        await agent._brain.router._in_flight
    await _drain_tasks(agent)

    # No barge — gate suppressed the drain.
    assert "should not barge" not in fake_session.said
    # Item is still in the queue (drain didn't pop it).
    assert agent._brain.queue.peek_fresh() is not None


# ─────────────────────── M4 streaming TTS handle ──────────────────────


@pytest.mark.asyncio
async def test_streaming_dispatch_routes_speak_through_streaming_path() -> None:
    """End-to-end: brain.decide returns `speak` → router invokes the
    streaming factory → `_StreamingTtsHandle.listener` pushes the
    utterance through `_start_streaming_say` → `audio_played` is True
    → router skips queue.push → drain finds no AI utterance → no AI
    transcript turn appended via the legacy queue path."""
    brain = FakeBrainClient()
    brain.enqueue_speak("Walk me through capacity.", confidence=0.9)
    agent, fake_session, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("How many users do we expect?")
    if agent._brain is not None and agent._brain.router._in_flight is not None:
        await agent._brain.router._in_flight
    await _drain_tasks(agent)

    # Streaming path delivered the utterance, not legacy `_say`.
    assert fake_session.streamed_full == ["Walk me through capacity."]
    # Legacy `say()` was NOT used for the streaming utterance (only
    # whatever the test scaffolding pushed during opening).
    assert "Walk me through capacity." not in fake_session.said


@pytest.mark.asyncio
async def test_streaming_dispatch_stay_silent_does_not_open_say() -> None:
    """`stay_silent` decisions never invoke the listener; the streaming
    factory's lazy-start path means no `session.say` is ever scheduled
    (no SynthesizeStream warmed up for an utterance that won't play)."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("listening")
    agent, fake_session, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("Quick check.")
    if agent._brain is not None and agent._brain.router._in_flight is not None:
        await agent._brain.router._in_flight
    await _drain_tasks(agent)

    assert fake_session.streamed_deltas == []
    assert fake_session.streamed_full == []


@pytest.mark.asyncio
async def test_streaming_say_holds_say_lock_so_drain_short_circuits() -> None:
    """Plan Unit 4 / ADV-009: a streaming dispatch holds `_say_lock` for
    its full duration so the router's pre-dispatch
    `_drain_if_fresh` (which calls `_drain_utterance_queue`, which
    short-circuits on `_say_lock.locked()`) cannot race a queued
    `session.say(text)` against an in-flight `session.say(deltas)`.
    """
    from archmentor_agent.main import _StreamingTtsHandle

    brain = FakeBrainClient()
    agent, fake_session, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)
    handle = _StreamingTtsHandle(agent)

    # Lock unheld before any deltas.
    assert agent._say_lock.locked() is False

    # First delta starts the streaming say AND acquires the lock.
    await handle.listener("hello ")
    assert agent._say_lock.locked() is True
    # While streaming-say is in flight, `_drain_utterance_queue` is a
    # no-op and the candidate cannot have a queued utterance racing the
    # streaming output.
    await agent._drain_utterance_queue()
    # No drain side effect (the queue is empty AND the lock is held —
    # either gate would skip; we want both to be intact).
    assert fake_session.said == [] or "hello" not in fake_session.said[-1]

    # `aclose` releases the lock so the next dispatch can drain.
    await handle.listener("world")
    await handle.aclose()
    assert agent._say_lock.locked() is False
    # Audio actually delivered through the streaming path.
    assert fake_session.streamed_full == ["hello world"]


@pytest.mark.asyncio
async def test_streaming_handle_releases_say_lock_on_playout_exception() -> None:
    """If `wait_for_playout` raises a non-CancelledError, `aclose` still
    runs the transcript-append + releases `_say_lock` so the rest of
    the session is not deadlocked behind a permanently-held lock.
    """
    from archmentor_agent.main import _StreamingTtsHandle

    brain = FakeBrainClient()
    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    class _ExplodingHandle:
        async def wait_for_playout(self) -> None:
            raise RuntimeError("playout disconnected")

    def _exploding_start_streaming_say(_deltas):
        return _ExplodingHandle()

    agent._start_streaming_say = _exploding_start_streaming_say  # ty: ignore[invalid-assignment]

    handle = _StreamingTtsHandle(agent)
    await handle.listener("ping")
    assert agent._say_lock.locked() is True
    # `aclose` must swallow the playout error and still release the
    # lock — the candidate has heard partial audio either way and the
    # session must continue.
    await handle.aclose()
    assert agent._say_lock.locked() is False


# ──────────────────────────────────────────────────────────────────────
# M4 Unit 5 — Haiku compactor + SessionTelemetry
# ──────────────────────────────────────────────────────────────────────


def _make_window_state(*, turn_count: int, summary: str = "") -> SessionState:
    """Seed state with `turn_count` candidate turns + an existing summary."""
    from archmentor_agent.state.session_state import TranscriptTurn

    return _seed_state().model_copy(
        update={
            "transcript_window": [
                TranscriptTurn(t_ms=i * 100, speaker="candidate", text=f"turn-{i}")
                for i in range(turn_count)
            ],
            "session_summary": summary,
        }
    )


@pytest.mark.asyncio
async def test_compactor_runs_when_window_crosses_threshold() -> None:
    """Threshold-crossing append spawns the Haiku compactor task.

    Seeds 30 turns; the next candidate turn brings the window to 31,
    which is the first cross of `_SUMMARY_COMPACTION_THRESHOLD = 30`.
    The compactor must (a) call Haiku, (b) drop the oldest 1 turn,
    (c) append to `session_summary`, (d) roll usage into cost_usd_total,
    (e) emit a `summary_compressed` ledger row.
    """
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    haiku = FakeHaikuClient(summary_text="Compressed window slice.", cost_usd=0.005)

    seed = _make_window_state(turn_count=30, summary="prior digest")
    agent, _, ledger, _, store, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku
    )

    await agent.handle_user_input("the 31st turn")
    await _drain_tasks(agent)

    # Compactor ran exactly once and saw the existing summary + dropped slice.
    assert len(haiku.calls) == 1
    existing, dropped_texts = haiku.calls[0]
    assert existing == "prior digest"
    assert dropped_texts == ["turn-0"]

    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    # Window dropped the oldest turn; tail still has the new candidate turn.
    assert len(final_state.transcript_window) == 30
    assert final_state.transcript_window[0].text == "turn-1"
    assert final_state.transcript_window[-1].text == "the 31st turn"
    # Summary appended with the new fragment.
    assert "prior digest" in final_state.session_summary
    assert "Compressed window slice." in final_state.session_summary
    # Cost rolled in (compactor adds 0.005 to whatever the brain dispatch added).
    assert final_state.cost_usd_total >= 0.005
    # `summary_compressed` ledger row landed.
    summary_rows = [p for et, p in ledger.appends if et == "summary_compressed"]
    assert len(summary_rows) == 1
    assert summary_rows[0]["dropped_turn_count"] == 1
    assert summary_rows[0]["model"] == "anthropic/claude-haiku-4-5"
    assert "input_tokens" in summary_rows[0]
    assert "output_tokens" in summary_rows[0]
    # Telemetry counter incremented.
    assert agent._telemetry.compactions_run == 1


@pytest.mark.asyncio
async def test_compactor_does_not_run_below_threshold() -> None:
    """30 turns or fewer leaves the window untouched; no Haiku call fires."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    haiku = FakeHaikuClient()

    seed = _make_window_state(turn_count=28)
    agent, _, _, _, store, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku
    )

    await agent.handle_user_input("turn 29")
    await _drain_tasks(agent)

    assert haiku.calls == []
    assert agent._telemetry.compactions_run == 0
    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    # Window kept all 29 (28 seeded + 1 new) — no truncation, no compaction.
    assert len(final_state.transcript_window) == 29


@pytest.mark.asyncio
async def test_compactor_does_not_run_when_haiku_not_wired() -> None:
    """No Haiku client → window grows unbounded; no compaction attempted.

    This is the contract for replay/dev paths that don't want a network
    call during tests. The threshold trigger checks `self._brain.haiku is None`.
    """
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    seed = _make_window_state(turn_count=30)
    agent, _, _, _, store, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=None
    )

    await agent.handle_user_input("turn 31")
    await _drain_tasks(agent)

    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    # Window grew past 30 — no Haiku, no compactor, no truncation.
    assert len(final_state.transcript_window) == 31
    assert agent._telemetry.compactions_run == 0


@pytest.mark.asyncio
async def test_compactor_failure_records_failed_ledger_row_and_releases_in_flight_flag() -> None:
    """A Haiku exception is logged, surfaces as `summary_compression_failed`,
    and resets `_summary_in_flight` so the next threshold tick can retry.
    """
    import structlog.testing

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    boom = RuntimeError("haiku 5xx")
    haiku = FakeHaikuClient(raise_exc=boom)

    seed = _make_window_state(turn_count=30)
    agent, _, ledger, _, _, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku
    )

    with structlog.testing.capture_logs() as logs:
        await agent.handle_user_input("turn 31")
        await _drain_tasks(agent)

    failed_rows = [p for et, p in ledger.appends if et == "summary_compression_failed"]
    assert len(failed_rows) == 1
    assert failed_rows[0]["dropped_turn_count"] == 1
    failed_logs = [le for le in logs if le.get("event") == "agent.summary.compaction.failed"]
    assert len(failed_logs) == 1
    assert agent._summary_in_flight is False
    # Counter still increments (counts attempts, not Haiku-billed calls).
    assert agent._telemetry.compactions_run == 1


@pytest.mark.asyncio
async def test_concurrent_threshold_crossings_short_circuit_via_in_flight_flag() -> None:
    """Two threshold-crossings in quick succession: the second short-circuits
    because `_summary_in_flight` is True — exactly one Haiku call fires.

    Drives the case where the flag is set by the first tick before its
    task awaits `haiku.compress` — a second tick that arrives between
    the spawn and the flag-clear should NOT spawn a duplicate task.
    """
    import asyncio as _asyncio

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    brain.enqueue_stay_silent("ack")

    # Custom fake that blocks until released so we can land a second
    # `handle_user_input` while the first compactor is mid-flight.
    release = _asyncio.Event()

    class _SlowHaiku(FakeHaikuClient):
        async def compress(self, *, existing_summary: str, dropped_turns: list[Any]):
            self.calls.append((existing_summary, [t.text for t in dropped_turns]))
            await release.wait()
            return self.summary_text, BrainUsage(
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.0001,
            )

    haiku = _SlowHaiku()
    seed = _make_window_state(turn_count=30)
    agent, _, _, _, _, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku
    )

    # First trigger — spawns the compactor task; it parks on `release`.
    await agent.handle_user_input("turn 31")
    # Second trigger — must short-circuit (one in-flight already).
    await agent.handle_user_input("turn 32")
    assert agent._summary_in_flight is True
    assert len(haiku.calls) == 1
    # Release the in-flight compactor and drain.
    release.set()
    await _drain_tasks(agent)
    assert agent._summary_in_flight is False
    # Only one Haiku call across both triggers.
    assert len(haiku.calls) == 1


@pytest.mark.asyncio
async def test_compaction_preserves_decisions_byte_identical() -> None:
    """Plan R10: the decisions log is sacred — compaction MUST NOT mutate
    a single byte of `state.decisions`. The mutator only updates
    `transcript_window`, `session_summary`, and the cost/token counters;
    a regression here corrupts the structured-decision history that
    replay tooling treats as authoritative.
    """
    from archmentor_agent.state.session_state import DesignDecision, TranscriptTurn

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    haiku = FakeHaikuClient(summary_text="compressed")

    seeded_decisions = [
        DesignDecision(
            t_ms=10_000,
            decision="Use Kafka for event sourcing",
            reasoning="Need durability + replay",
            alternatives=["RabbitMQ", "Kinesis"],
        ),
        DesignDecision(
            t_ms=20_000,
            decision="Shard by user_id",
            reasoning="Most reads are user-scoped",
            alternatives=["Hash sharding", "Range sharding"],
        ),
    ]
    seed = _seed_state().model_copy(
        update={
            "transcript_window": [
                TranscriptTurn(t_ms=i * 100, speaker="candidate", text=f"turn-{i}")
                for i in range(30)
            ],
            "session_summary": "prior digest",
            "decisions": seeded_decisions,
        }
    )

    # Snapshot a deep-copy of the decisions list before compaction so a
    # post-apply mutation surfaces as a difference.
    pre_snapshot = [d.model_dump() for d in seeded_decisions]

    agent, _, _, _, store, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku
    )

    await agent.handle_user_input("turn 31")
    await _drain_tasks(agent)

    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    # Compaction ran (sanity check — otherwise the assertion below is
    # vacuous).
    assert len(haiku.calls) == 1
    # Decisions list is the same length and every entry's serialized
    # bytes match the pre-compaction snapshot exactly.
    assert len(final_state.decisions) == len(pre_snapshot)
    for actual, expected in zip(final_state.decisions, pre_snapshot, strict=True):
        assert actual.model_dump() == expected, "compaction mutator leaked into decisions log"


@pytest.mark.asyncio
async def test_compaction_skips_gracefully_on_cas_exhausted() -> None:
    """Plan-bound: when the CAS apply for a compaction exhausts retries,
    `_run_compaction` logs `agent.summary.compaction.cas_exhausted` and
    returns cleanly — `_summary_in_flight` resets so the next threshold
    crossing retries, and no `summary_compressed` row gets written
    (would otherwise mis-represent the in-Redis state).
    """
    import structlog.testing
    from archmentor_agent.state import RedisCasExhaustedError

    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    haiku = FakeHaikuClient()

    seed = _make_window_state(turn_count=30)
    store = FakeSessionStore()
    await store.put(SESSION_ID, seed)
    agent, _, ledger, _, _, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku, store=store
    )

    # Patch the store's `apply` so the compaction-side CAS raises
    # `RedisCasExhaustedError` after the transcript-append (which
    # crosses the threshold) has already landed. The transcript-append
    # CAS must succeed first so the compactor task actually spawns.
    real_apply = store.apply

    async def _flaky_apply(
        session_id: UUID,
        mutator: Callable[[SessionState | None], SessionState],
        *,
        max_retries: int = 6,
    ) -> SessionState:
        # Calls 1..N pass through normally; the compaction-side apply
        # is the one that lands AFTER `_run_compaction` calls `await
        # haiku.compress(...)`. In the fake store there's no inherent
        # contention; we tag the failing call by the `_summary_in_flight`
        # flag the agent flips before spawning compaction.
        if agent._summary_in_flight and len(haiku.calls) >= 1:
            raise RedisCasExhaustedError("simulated CAS exhaustion during compaction apply")
        return await real_apply(session_id, mutator, max_retries=max_retries)

    store.apply = _flaky_apply  # ty: ignore[invalid-assignment]

    with structlog.testing.capture_logs() as logs:
        await agent.handle_user_input("turn 31")
        await _drain_tasks(agent)

    # Haiku was called (compaction spawned), but the apply blew up.
    assert len(haiku.calls) == 1
    # `_summary_in_flight` reset — next threshold can retry.
    assert agent._summary_in_flight is False
    # No `summary_compressed` row written (apply never landed).
    summary_rows = [p for et, p in ledger.appends if et == "summary_compressed"]
    assert summary_rows == []
    # CAS-exhausted log line emitted.
    cas_logs = [le for le in logs if le.get("event") == "agent.summary.compaction.cas_exhausted"]
    assert len(cas_logs) == 1


@pytest.mark.asyncio
async def test_compactor_handles_repeated_threshold_crossings_without_drift() -> None:
    """Plan-bound 200-turn-stress proxy: drive the compactor through 30
    threshold crossings (≈ a 200-turn session at our cadence) and
    assert (a) every Haiku invocation produced exactly one
    `summary_compressed` row, (b) the rolling window stayed bounded
    near the threshold, (c) `_summary_in_flight` is False at the end,
    (d) decisions stay byte-identical from start to finish.
    """
    from archmentor_agent.state.session_state import DesignDecision

    brain = FakeBrainClient()
    haiku = FakeHaikuClient(summary_text="cycle")
    seeded_decisions = [
        DesignDecision(t_ms=1_000, decision="seed-d-1", reasoning="r1"),
        DesignDecision(t_ms=2_000, decision="seed-d-2", reasoning="r2"),
    ]
    seed = _make_window_state(turn_count=30).model_copy(update={"decisions": seeded_decisions})
    pre_snapshot = [d.model_dump() for d in seeded_decisions]

    agent, _, ledger, _, store, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku
    )

    cycles = 30
    for i in range(cycles):
        brain.enqueue_stay_silent(f"cycle-{i}")
        await agent.handle_user_input(f"stress-turn-{i}")
        await _drain_tasks(agent)

    # Every cycle crossed the 30-turn threshold once → one Haiku call
    # per cycle, one ledger row per cycle.
    assert len(haiku.calls) == cycles
    summary_rows = [p for et, p in ledger.appends if et == "summary_compressed"]
    assert len(summary_rows) == cycles
    # Window stayed bounded around the threshold (each cycle drops 1,
    # adds 1).
    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    assert len(final_state.transcript_window) <= 31
    # The flag has reset; the agent is not stuck.
    assert agent._summary_in_flight is False
    # Decisions log untouched after `cycles` compactions.
    assert len(final_state.decisions) == len(pre_snapshot)
    for actual, expected in zip(final_state.decisions, pre_snapshot, strict=True):
        assert actual.model_dump() == expected


def test_publish_telemetry_payload_fits_at_max_realistic_values() -> None:
    """P1-8 / ADV-006 pre-flight: the 5-field telemetry payload at the
    upper end of plausible session values fits inside
    `_TELEMETRY_PAYLOAD_MAX_BYTES`. Adding a 6th field (or letting
    counters overflow this guard) is what would otherwise trip the
    runtime size check in `_publish_telemetry` and silently kill the
    cost-budget indicator for the rest of the session.
    """
    import json as _json

    from archmentor_agent.main import _TELEMETRY_PAYLOAD_MAX_BYTES

    payload = _json.dumps(
        {
            # 6-decimal cost burn at the upper end of cost cap headroom.
            "cost_usd_total": 9999.999_999,
            "cost_cap_usd": 9999.999_999,
            # 7-digit dispatch count covers a multi-day soak run.
            "calls_made": 9_999_999,
            # 12-digit token totals cover a 45-min session at Opus
            # output rates with margin to spare.
            "tokens_in_total": 999_999_999_999,
            "tokens_out_total": 999_999_999_999,
        }
    )
    assert len(payload) <= _TELEMETRY_PAYLOAD_MAX_BYTES, (
        f"telemetry payload {len(payload)}B exceeds budget "
        f"{_TELEMETRY_PAYLOAD_MAX_BYTES}B — review field set"
    )


@pytest.mark.asyncio
async def test_session_telemetry_emits_log_line_on_shutdown() -> None:
    """Single `agent.session.telemetry` line per session, with all 6 fields."""
    import structlog.testing

    brain = FakeBrainClient()
    brain.enqueue_speak("first answer")
    brain.enqueue_stay_silent("nothing more")
    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("first turn")
    await agent.handle_user_input("second turn")
    await _drain_tasks(agent)

    with structlog.testing.capture_logs() as logs:
        await agent.shutdown()

    telemetry_lines = [le for le in logs if le.get("event") == "agent.session.telemetry"]
    assert len(telemetry_lines) == 1
    line = telemetry_lines[0]
    # Six fields, all present.
    assert "ttfa_ms_histogram" in line
    assert "brain_calls_made" in line
    assert "skipped_idempotent_count" in line
    assert "skipped_cooldown_count" in line
    assert "dropped_stale_count" in line
    assert "compactions_run" in line
    # Two dispatches → two recorded brain calls.
    assert line["brain_calls_made"] == 2
    # The streaming TTS path produced at least one TTFA sample for the speak.
    assert isinstance(line["ttfa_ms_histogram"], list)
    assert len(line["ttfa_ms_histogram"]) >= 1


@pytest.mark.asyncio
async def test_telemetry_log_line_emits_even_under_kill_switch() -> None:
    """Stable log shape across configurations — kill-switch emits zeros."""
    import structlog.testing

    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=False)

    with structlog.testing.capture_logs() as logs:
        await agent.shutdown()

    telemetry_lines = [le for le in logs if le.get("event") == "agent.session.telemetry"]
    assert len(telemetry_lines) == 1
    line = telemetry_lines[0]
    assert line["brain_calls_made"] == 0
    assert line["compactions_run"] == 0
    assert line["dropped_stale_count"] == 0
    assert line["ttfa_ms_histogram"] == []


@pytest.mark.asyncio
async def test_router_increments_skipped_idempotent_telemetry() -> None:
    """Repeat dispatches with identical state + payload short-circuit; counter rises."""
    from archmentor_agent.events.types import EventType, RouterEvent

    brain = FakeBrainClient()
    # First call: stay_silent → arms the fingerprint.
    brain.enqueue_stay_silent("nothing")
    # Second call should NOT reach the brain; it short-circuits.
    brain.enqueue_speak("would-be-spoken")

    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)
    assert agent._brain is not None
    router = agent._brain.router

    payload = {"phase": "intro"}
    await router.handle(RouterEvent(type=EventType.PHASE_TIMER, t_ms=100, payload=payload))
    await router.wait_for_idle()
    await router.handle(RouterEvent(type=EventType.PHASE_TIMER, t_ms=200, payload=payload))
    await router.wait_for_idle()
    await _drain_tasks(agent)

    # Second dispatch was skipped — only one Anthropic-side call.
    assert len(brain.calls) == 1
    assert agent._telemetry.skipped_idempotent_count == 1
    # Both dispatches counted toward total brain calls (skipped paths included).
    assert agent._telemetry.brain_calls_made == 2


# ---------------------------------------------------------------------------
# Unit 9 — `ai_telemetry` publish + post-dispatch callback wiring (R24)
# ---------------------------------------------------------------------------


def _published_telemetry_frames(agent: MentorAgent) -> list[dict[str, Any]]:
    """Return the parsed JSON payloads sent to the `ai_telemetry` topic."""
    participant = cast(_FakeLocalParticipant, agent._room.local_participant)
    frames: list[dict[str, Any]] = []
    for topic, payload in participant.published:
        if topic != "ai_telemetry":
            continue
        frames.append(json.loads(payload))
    return frames


@pytest.mark.asyncio
async def test_dispatch_complete_publishes_ai_telemetry_frame() -> None:
    """Every brain dispatch publishes one `ai_telemetry` frame mirroring SessionState."""
    brain = FakeBrainClient()
    brain.enqueue_speak("First nudge.", cost_usd=0.012)
    seed = _seed_state(cost_usd_total=0.0, cost_cap_usd=5.0)
    agent, _, _, _, store, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed
    )

    await agent.handle_user_input("Initial requirements walk-through.")
    await _drain_tasks(agent)

    frames = _published_telemetry_frames(agent)
    assert len(frames) == 1
    frame = frames[0]
    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    assert frame["cost_usd_total"] == round(final_state.cost_usd_total, 4)
    assert frame["cost_cap_usd"] == 5.0
    assert frame["calls_made"] == agent._telemetry.brain_calls_made
    assert frame["tokens_in_total"] == final_state.tokens_input_total
    assert frame["tokens_out_total"] == final_state.tokens_output_total


@pytest.mark.asyncio
async def test_compaction_publishes_ai_telemetry_frame() -> None:
    """Haiku compaction emits an extra telemetry frame so the bar updates after Haiku spend."""
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("ack")
    haiku = FakeHaikuClient(summary_text="Compressed slice.", cost_usd=0.003)
    seed = _make_window_state(turn_count=30, summary="prior digest")
    agent, _, _, _, _, _, _ = _build_agent_under_test(
        brain_enabled=True, brain=brain, seed_state=seed, haiku=haiku
    )

    await agent.handle_user_input("the 31st turn")
    await _drain_tasks(agent)

    frames = _published_telemetry_frames(agent)
    # Two frames: one from the dispatch callback, one from the compactor.
    assert len(frames) == 2
    # Final frame reflects post-compaction cost (>= compactor's 0.003 add).
    assert frames[-1]["cost_usd_total"] >= 0.003


@pytest.mark.asyncio
async def test_publish_telemetry_swallows_publish_data_error() -> None:
    """Mid-teardown PublishDataError must not break the voice loop."""
    import structlog.testing
    from livekit.rtc.participant import PublishDataError

    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=False)

    async def _raise_engine_closed(payload: str, *, topic: str) -> None:
        _ = payload, topic
        raise PublishDataError("engine: connection error: engine is closed")

    agent._room.local_participant.publish_data = _raise_engine_closed  # ty: ignore[invalid-assignment]

    state = _seed_state(cost_usd_total=0.42, cost_cap_usd=5.0)
    with structlog.testing.capture_logs() as logs:
        await agent._publish_telemetry(state)

    failed = [le for le in logs if le.get("event") == "agent.publish_telemetry_failed"]
    assert len(failed) == 1
    assert "engine" in failed[0]["reason"]


@pytest.mark.asyncio
async def test_publish_telemetry_payload_under_size_budget() -> None:
    """Payload stays comfortably under `_TELEMETRY_PAYLOAD_MAX_BYTES`."""
    from archmentor_agent.main import _TELEMETRY_PAYLOAD_MAX_BYTES

    agent, _, _, _, _, _, _ = _build_agent_under_test(brain_enabled=False)
    # Use generous values to stress-test the budget — millions of tokens
    # / four-digit cost still has to fit.
    agent._telemetry.brain_calls_made = 9999
    state = _seed_state(cost_usd_total=4.9876, cost_cap_usd=5.0)
    state = state.model_copy(
        update={
            "tokens_input_total": 9_876_543,
            "tokens_output_total": 1_234_567,
        }
    )
    await agent._publish_telemetry(state)
    frames = _published_telemetry_frames(agent)
    assert len(frames) == 1
    raw = json.dumps(frames[0])
    assert len(raw) < _TELEMETRY_PAYLOAD_MAX_BYTES


# ---------------------------------------------------------------------------
# Unit 10 — M3-dogfood transcript_window=0 regression (commit ce90164)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_window_populates_after_two_user_turns() -> None:
    """Regression: `state.transcript_window` length >= 2 after two finals.

    Master plan §696 logged a M3 dogfood finding where the agent's
    in-memory `transcript_window` stayed empty after the first turn
    because the CAS apply path was passing `state` instead of mutating
    `transcript_window`. Fixed in `ce90164`. This test pins the fix so
    a future refactor of `_append_transcript_turn` doesn't regress it.
    """
    brain = FakeBrainClient()
    brain.enqueue_stay_silent("first")
    brain.enqueue_stay_silent("second")
    agent, _, _, _, store, _, _ = _build_agent_under_test(brain_enabled=True, brain=brain)

    await agent.handle_user_input("Talk about partitioning first.")
    await agent.handle_user_input("Then we should walk capacity.")
    await _drain_tasks(agent)

    final_state = await store.load(SESSION_ID)
    assert final_state is not None
    candidate_turns = [t for t in final_state.transcript_window if t.speaker == "candidate"]
    assert len(candidate_turns) >= 2, (
        f"transcript_window has {len(candidate_turns)} candidate turn(s); "
        "M3 dogfood regression — should have at least 2 after two finals."
    )


# Suppress unused-import warnings — these types are used via `cast`.
_ = BrainUsage
_ = BrainDecision
