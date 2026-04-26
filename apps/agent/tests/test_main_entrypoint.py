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
    """

    def __init__(self, *, raise_on_say: bool = False) -> None:
        self.said: list[str] = []
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


def _build_agent_under_test(
    *,
    brain_enabled: bool,
    brain: FakeBrainClient | None = None,
    store: FakeSessionStore | None = None,
    snapshots: FakeSnapshotClient | None = None,
    canvas_snapshots: FakeCanvasSnapshotClient | None = None,
    seed_state: SessionState | None = None,
    session_raises: bool = False,
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
    # Prime the t0 clock so `_now_relative_ms` returns deterministic
    # values. `on_enter` would normally do this.
    agent._t0_ms = 0
    return agent, fake_session, ledger, brain, store, snapshots, canvas_snapshots


async def _drain_tasks(agent: MentorAgent) -> None:
    if agent._snapshot_tasks:
        await asyncio.gather(*agent._snapshot_tasks, return_exceptions=True)
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

    assert fake_session.said == ["Tell me how you'd index the short codes."]
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

    assert fake_session.said == ["probe 1", "probe 2", "probe 3"]
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


# Suppress unused-import warnings — these types are used via `cast`.
_ = BrainUsage
_ = BrainDecision
