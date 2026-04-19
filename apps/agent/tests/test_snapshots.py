"""Brain-snapshot serialization unit tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from archmentor_agent.snapshots import build_snapshot
from archmentor_agent.state import DesignDecision, SessionState
from archmentor_agent.state.session_state import ProblemCard


def _state() -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="Design a URL shortener",
            statement_md="# Design",
            rubric_yaml="dimensions: []",
        ),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
        decisions=[
            DesignDecision(
                t_ms=120_000,
                decision="Use Kafka for event sourcing",
                reasoning="Durability + replay",
                alternatives=["RabbitMQ"],
            )
        ],
    )


def test_build_snapshot_captures_full_state() -> None:
    session_id = uuid4()
    snapshot = build_snapshot(
        session_id=session_id,
        t_ms=1_500,
        state=_state(),
        event_payload={"type": "turn_end", "transcript": "..."},
        brain_output={"decision": "speak", "utterance": "What's your QPS target?"},
        reasoning="Candidate skipped capacity; nudge.",
        tokens_input=800,
        tokens_output=42,
    )

    assert snapshot["session_id"] == str(session_id)
    assert snapshot["t_ms"] == 1_500
    assert snapshot["reasoning_text"] == "Candidate skipped capacity; nudge."
    assert snapshot["tokens_input"] == 800
    assert snapshot["tokens_output"] == 42
    assert snapshot["brain_output_json"]["decision"] == "speak"
    assert snapshot["event_payload_json"]["type"] == "turn_end"

    decisions = snapshot["session_state_json"]["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "Use Kafka for event sourcing"


def test_build_snapshot_is_json_serializable() -> None:
    snapshot = build_snapshot(
        session_id=uuid4(),
        t_ms=0,
        state=_state(),
        event_payload={},
        brain_output={},
    )
    # The row is handed to SQLAlchemy/JSONB; it must survive a round trip.
    encoded = json.dumps(snapshot)
    assert json.loads(encoded) == snapshot


def test_build_snapshot_rejects_negative_t_ms() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        build_snapshot(
            session_id=uuid4(),
            t_ms=-1,
            state=_state(),
            event_payload={},
            brain_output={},
        )


def test_build_snapshot_rejects_negative_tokens() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        build_snapshot(
            session_id=uuid4(),
            t_ms=0,
            state=_state(),
            event_payload={},
            brain_output={},
            tokens_input=-5,
        )
