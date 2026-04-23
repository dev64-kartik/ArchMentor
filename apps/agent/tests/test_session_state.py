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


def test_with_state_updates_translates_brain_subkeys() -> None:
    """Tool-schema sub-keys (phase_advance, new_decision, etc.) must
    map to real SessionState fields. A plain model_copy(update=...)
    would silently drop them because the names don't match.
    """
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
    )

    updated = state.with_state_updates(
        {
            "phase_advance": "requirements",
            "rubric_coverage_delta": {
                "capacity": {"covered": True, "depth": "shallow", "last_touched_t_ms": 1000},
            },
            "new_decision": {
                "t_ms": 42000,
                "decision": "Use Kafka for event sourcing",
                "reasoning": "Need durability + replay",
                "alternatives": ["RabbitMQ"],
            },
            "session_summary_append": "Candidate grounded the capacity question.",
        }
    )

    assert updated.phase is InterviewPhase.REQUIREMENTS
    assert updated.rubric_coverage["capacity"].covered is True
    assert len(updated.decisions) == 1
    assert updated.decisions[0].decision == "Use Kafka for event sourcing"
    assert "Candidate grounded" in updated.session_summary

    # Original instance untouched — translator is pure.
    assert state.phase is InterviewPhase.INTRO
    assert state.decisions == []


def test_with_state_updates_is_a_noop_on_empty() -> None:
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
    )
    assert state.with_state_updates({}) is state


def test_with_state_updates_ignores_null_subkeys() -> None:
    """Absent or null sub-keys mean "no change" — preserves
    backward-compat when the brain emits a partial state_updates dict.
    """
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
    )

    updated = state.with_state_updates({"phase_advance": None, "new_decision": None})

    assert updated.phase is InterviewPhase.INTRO
    assert updated.decisions == []


def test_with_state_updates_appends_to_existing_summary() -> None:
    """session_summary_append concatenates with a blank-line separator
    so repeated appends produce a readable running summary rather than
    a single run-on paragraph.
    """
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
        session_summary="First beat.",
    )
    updated = state.with_state_updates({"session_summary_append": "Second beat."})
    assert updated.session_summary == "First beat.\n\nSecond beat."
