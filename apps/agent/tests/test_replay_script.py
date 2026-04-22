"""Integration tests for `scripts/replay.py`.

The script spans the API (loads a `brain_snapshots` row from Postgres
via SQLModel) and the agent (runs the fresh decision through
`BrainClient`). Tests mock both boundaries:

- `_load_snapshot` returns a synthesized `BrainSnapshot` so we never
  touch a real database.
- `brain_factory` is overridden to return a `FakeBrainClient` so no
  Anthropic calls leak out of the test run.

Covers the four documented exit codes (`EXIT_MATCH`, `EXIT_DIVERGED`,
`EXIT_NOT_FOUND`) plus the placeholder-key fail-closed path.
"""

from __future__ import annotations

import os

# Set API-side env vars BEFORE any `archmentor_api` import runs. The
# agent `conftest.py` seeds agent-prefixed vars; the API's own
# conftest isn't picked up for this test file, so settings construction
# would blow up on `API_JWT_SECRET` missing. Duplicating the seed here
# is cheaper than cross-importing another conftest.
os.environ.setdefault("API_JWT_SECRET", "test_secret_test_secret_test_secret_test_secret")
os.environ.setdefault("API_JWT_ISSUER", "http://localhost:9999")
os.environ.setdefault("API_LIVEKIT_API_KEY", "devkey")
os.environ.setdefault(
    "API_LIVEKIT_API_SECRET", "test_lk_secret_test_lk_secret_test_lk_secret_test_lk"
)
os.environ.setdefault(
    "API_AGENT_INGEST_TOKEN", "test_agent_token_test_agent_token_test_agent_token"
)

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from _helpers import FakeBrainClient
from archmentor_agent.brain.client import BrainClient
from archmentor_agent.brain.decision import BrainDecision, BrainUsage
from archmentor_agent.state.session_state import ProblemCard, SessionState


def _load_replay_module() -> Any:
    """Load `scripts/replay.py` directly by path.

    Pytest's `importlib` mode doesn't add `scripts/` to sys.path, and
    this test file runs under the agent test package. Loading by path
    sidesteps both issues without polluting sys.path globally.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    replay_path = repo_root / "scripts" / "replay.py"
    spec = importlib.util.spec_from_file_location("_replay_cli", replay_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so `@dataclass` decorators
    # inside the module can resolve their own `cls.__module__` back to
    # the live module dict. Missing this registration raises
    # `AttributeError: 'NoneType' object has no attribute '__dict__'`
    # from `dataclasses._is_type`.
    sys.modules["_replay_cli"] = module
    spec.loader.exec_module(module)
    return module


replay = _load_replay_module()


SNAPSHOT_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
SESSION_ID = UUID("11111111-2222-3333-4444-555555555555")


class _FakeBrainSnapshot:
    """Duck-typed stand-in for `BrainSnapshot`.

    `run_replay` reads five attributes off the row: `session_id`,
    `t_ms`, `session_state_json`, `event_payload_json`,
    `brain_output_json`. Nothing else. Skipping the SQLModel class
    keeps tests from needing a live DB engine.
    """

    def __init__(
        self,
        *,
        session_id: UUID,
        t_ms: int,
        session_state_json: dict[str, Any],
        event_payload_json: dict[str, Any],
        brain_output_json: dict[str, Any],
    ) -> None:
        self.session_id = session_id
        self.t_ms = t_ms
        self.session_state_json = session_state_json
        self.event_payload_json = event_payload_json
        self.brain_output_json = brain_output_json


def _make_session_state() -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="URL Shortener",
            statement_md="Design a URL shortener.",
            rubric_yaml="dimensions: []\n",
        ),
        system_prompt_version="m2-initial",
        started_at=datetime(2026, 4, 22, tzinfo=UTC),
        elapsed_s=60,
        remaining_s=2640,
    )


def _make_snapshot(
    *,
    stored_decision: str = "speak",
    stored_priority: str = "medium",
    stored_confidence: float = 0.8,
    stored_utterance: str | None = "How would you shard the code table?",
    stored_reasoning: str = "Probe their sharding story.",
) -> _FakeBrainSnapshot:
    state = _make_session_state()
    return _FakeBrainSnapshot(
        session_id=SESSION_ID,
        t_ms=5_000,
        session_state_json=state.model_dump(mode="json"),
        event_payload_json={"type": "turn_end", "t_ms": 5_000, "text": "my idea"},
        brain_output_json={
            "decision": stored_decision,
            "priority": stored_priority,
            "confidence": stored_confidence,
            "utterance": stored_utterance,
            "reasoning": stored_reasoning,
        },
    )


@pytest.fixture
def patch_load_snapshot(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a helper that swaps in a scripted snapshot loader."""

    def _install(snapshot_by_id: dict[UUID, _FakeBrainSnapshot | None]) -> None:
        def fake_load(raw_id: str) -> _FakeBrainSnapshot | None:
            return snapshot_by_id.get(UUID(raw_id))

        monkeypatch.setattr(replay, "_load_snapshot", fake_load)

    return _install


def test_dry_run_prints_preview_without_calling_brain(
    patch_load_snapshot: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default (`run=False`) path — preview only, no Anthropic traffic."""
    snap = _make_snapshot()
    patch_load_snapshot({SNAPSHOT_ID: snap})
    brain = FakeBrainClient()

    code = replay.run_replay(
        str(SNAPSHOT_ID),
        run=False,
        brain_factory=lambda: cast(BrainClient, brain),
    )

    assert code == replay.EXIT_MATCH
    assert brain.calls == []
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "system block" in out.lower()


def test_run_replay_matching_decision_returns_exit_0(
    patch_load_snapshot: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snap = _make_snapshot()
    patch_load_snapshot({SNAPSHOT_ID: snap})

    brain = FakeBrainClient()
    # Fresh decision matches stored on the three match-keys.
    brain.enqueue(
        BrainDecision(
            decision="speak",
            priority="medium",
            confidence=0.8,
            reasoning="Probe their sharding story.",
            utterance="How would you shard the code table?",
            usage=BrainUsage(input_tokens=50, output_tokens=20),
        )
    )

    code = replay.run_replay(
        str(SNAPSHOT_ID),
        run=True,
        brain_factory=lambda: cast(BrainClient, brain),
    )

    assert code == replay.EXIT_MATCH
    assert len(brain.calls) == 1
    out = capsys.readouterr().out
    assert "match_keys_agree: True" in out


def test_run_replay_confidence_drift_returns_exit_1(
    patch_load_snapshot: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snap = _make_snapshot(stored_confidence=0.8)
    patch_load_snapshot({SNAPSHOT_ID: snap})

    brain = FakeBrainClient()
    # Same decision + priority, different confidence → diverge.
    brain.enqueue(
        BrainDecision(
            decision="speak",
            priority="medium",
            confidence=0.6,
            reasoning="Probe their sharding story.",
            utterance="How would you shard the code table?",
            usage=BrainUsage(),
        )
    )

    code = replay.run_replay(
        str(SNAPSHOT_ID),
        run=True,
        brain_factory=lambda: cast(BrainClient, brain),
    )

    assert code == replay.EXIT_DIVERGED
    out = capsys.readouterr().out
    assert "match_keys_agree: False" in out


def test_missing_snapshot_returns_exit_2(
    patch_load_snapshot: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_load_snapshot({SNAPSHOT_ID: None})

    code = replay.run_replay(
        str(SNAPSHOT_ID),
        run=False,
        brain_factory=lambda: cast(BrainClient, FakeBrainClient()),
    )

    assert code == replay.EXIT_NOT_FOUND
    err = capsys.readouterr().err
    assert "snapshot not found" in err


def test_session_mode_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--session` replay is reserved — argparse must bail with SystemExit."""
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "sk-ant-test-fixture-not-a-real-key")
    with pytest.raises(SystemExit, match="reserved"):
        replay.main(["--session", str(uuid4())])


def test_run_refuses_placeholder_api_key(
    patch_load_snapshot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed if the API key on the shell is the `.env.example` stub.

    Without this, a well-meaning operator running `--run` with a fresh
    clone would burn a snapshot through a broken key and see an
    `anthropic.AuthenticationError` mid-call instead of a clean error
    at the CLI boundary.
    """
    snap = _make_snapshot()
    patch_load_snapshot({SNAPSHOT_ID: snap})
    monkeypatch.setenv("ARCHMENTOR_ANTHROPIC_API_KEY", "replace_with_real_key")

    with pytest.raises(SystemExit, match="placeholder"):
        replay.run_replay(
            str(SNAPSHOT_ID),
            run=True,
            brain_factory=lambda: cast(BrainClient, FakeBrainClient()),
        )


def test_invalid_uuid_exits_loudly() -> None:
    """Typos on the CLI should surface as a clear SystemExit, not a 500.

    Uses production `_load_snapshot` (not the fixture patch) so the
    real UUID validation path is exercised. It raises before the DB
    is touched, so no engine is required.
    """
    with pytest.raises(SystemExit, match="must be a UUID"):
        replay.run_replay(
            "not-a-uuid",
            run=False,
            brain_factory=lambda: cast(BrainClient, FakeBrainClient()),
        )
