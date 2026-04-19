from datetime import UTC, datetime

from archmentor_agent.state import DesignDecision, InterviewPhase, SessionState
from archmentor_agent.state.session_state import ProblemCard


def _problem() -> ProblemCard:
    return ProblemCard(
        slug="url-shortener",
        version=1,
        title="Design a URL shortener",
        statement_md="# Design a URL shortener\n\nWrite-heavy, low-latency reads...",
        rubric_yaml="dimensions: []",
    )


def test_session_state_defaults() -> None:
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
    )
    assert state.phase is InterviewPhase.INTRO
    assert state.remaining_s == 2700
    assert state.decisions == []
    assert state.pending_utterance is None


def test_decisions_are_never_null() -> None:
    decision = DesignDecision(
        t_ms=120_000,
        decision="Use Kafka for event sourcing",
        reasoning="Need durability + replay",
        alternatives=["RabbitMQ", "SQS"],
    )
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
        decisions=[decision],
    )
    assert state.decisions[0].decision.startswith("Use Kafka")
    assert "RabbitMQ" in state.decisions[0].alternatives
