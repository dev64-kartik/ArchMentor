from datetime import UTC, datetime

from archmentor_agent.state import DesignDecision, InterviewPhase, SessionState
from archmentor_agent.state.session_state import ActiveArgument, ProblemCard


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


def test_with_state_updates_coerces_bare_depth_strings() -> None:
    """Opus reliably emits `rubric_coverage_delta` with bare depth strings
    (`{"storage_design": "shallow"}`) instead of full CoverageStatus
    objects. The M3 dogfood (2026-04-25) hit ValidationError on every
    PG-on-canvas turn, which rolled back the entire dispatch including
    co-located `session_summary_append`. The apply path must coerce
    shorthand without losing siblings.
    """
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
    )

    updated = state.with_state_updates(
        {
            "rubric_coverage_delta": {"storage_design": "shallow"},
            "session_summary_append": "Candidate added Postgres on canvas.",
        }
    )

    coverage = updated.rubric_coverage["storage_design"]
    assert coverage.depth == "shallow"
    assert coverage.covered is True
    assert "Postgres on canvas" in updated.session_summary


def test_with_state_updates_treats_unknown_depth_as_shallow() -> None:
    """Off-spec depth strings shouldn't raise — coverage gets recorded as
    `shallow` so the dispatch still lands and the brain has a chance to
    correct itself on the next turn.
    """
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
    )
    updated = state.with_state_updates({"rubric_coverage_delta": {"capacity": "deep"}})
    assert updated.rubric_coverage["capacity"].depth == "shallow"
    assert updated.rubric_coverage["capacity"].covered is True


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


# ──────────────────────────────────────────────────────────────────────
# M4 Unit 7 — phase soft budgets, last_phase_change_s, helper logic
# ──────────────────────────────────────────────────────────────────────


def test_phase_advance_refreshes_last_phase_change_s_from_elapsed_s_fallback() -> None:
    """Replay path (no ``now_ms``): ``last_phase_change_s`` falls back
    to ``elapsed_s`` so old M2/M3-era snapshots still translate.
    """
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
        elapsed_s=420,  # 7 minutes into the session
        last_phase_change_s=0,
    )
    updated = state.with_state_updates({"phase_advance": "capacity"})
    assert updated.phase is InterviewPhase.CAPACITY
    assert updated.last_phase_change_s == 420


def test_phase_advance_uses_now_ms_when_provided() -> None:
    """Production path: ``now_ms`` (passed by the router) wins over
    ``elapsed_s``. Regression test for a real bug — ``elapsed_s`` is a
    dead field (seeded to 0, never updated), so without the ``now_ms``
    branch, every post-INTRO phase nudge fired immediately because the
    anchor never advanced past 0.
    """
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
        elapsed_s=0,  # the production-truthful value (dead field)
        last_phase_change_s=0,
    )
    updated = state.with_state_updates(
        {"phase_advance": "capacity"},
        now_ms=600_000,  # 10 minutes into the session
    )
    assert updated.phase is InterviewPhase.CAPACITY
    assert updated.last_phase_change_s == 600  # now_ms // 1000


def test_phase_advance_absent_keeps_last_phase_change_s() -> None:
    state = SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
        elapsed_s=600,
        last_phase_change_s=180,
        phase=InterviewPhase.REQUIREMENTS,
    )
    updated = state.with_state_updates({"new_decision": None})
    assert updated.last_phase_change_s == 180
    assert updated.phase is InterviewPhase.REQUIREMENTS


def test_phase_soft_budgets_sum_matches_session_duration() -> None:
    """Pin the budget split so a future tweak forces an explicit decision
    rather than silently drifting away from the 45-min session length.
    """
    from archmentor_agent.state.session_state import PHASE_SOFT_BUDGETS_S

    assert sum(PHASE_SOFT_BUDGETS_S.values()) == 2700


def test_should_nudge_requires_50pct_overrun() -> None:
    from archmentor_agent.state.session_state import _should_nudge

    # Exactly at budget * 1.5 → not yet nudged (strict ">" boundary).
    assert (
        _should_nudge(elapsed_in_phase_s=180, budget_s=120, last_nudge_s=-10_000, now_s=180)
        is False
    )
    # Past the boundary → nudge.
    assert (
        _should_nudge(elapsed_in_phase_s=181, budget_s=120, last_nudge_s=-10_000, now_s=181) is True
    )


def test_should_nudge_dedup_window_blocks_repeats() -> None:
    """A nudge fired 30 s ago must be suppressed; 91 s ago is past the gate."""
    from archmentor_agent.state.session_state import _should_nudge

    assert (
        _should_nudge(elapsed_in_phase_s=200, budget_s=120, last_nudge_s=170, now_s=200) is False
    )  # 30 s gap < 90 s
    assert (
        _should_nudge(elapsed_in_phase_s=300, budget_s=120, last_nudge_s=200, now_s=300) is True
    )  # 100 s gap > 90 s


def test_should_nudge_zero_budget_is_no_op() -> None:
    """Defensive: a missing PHASE_SOFT_BUDGETS_S entry must not nudge."""
    from archmentor_agent.state.session_state import _should_nudge

    assert (
        _should_nudge(elapsed_in_phase_s=10_000, budget_s=0, last_nudge_s=-10_000, now_s=10_000)
        is False
    )


def test_bucket_over_budget_pct_tiers() -> None:
    from archmentor_agent.state.session_state import _bucket_over_budget_pct

    assert _bucket_over_budget_pct(0) == 50
    assert _bucket_over_budget_pct(50) == 50
    assert _bucket_over_budget_pct(99) == 50
    assert _bucket_over_budget_pct(100) == 100
    assert _bucket_over_budget_pct(150) == 100
    assert _bucket_over_budget_pct(199) == 100
    assert _bucket_over_budget_pct(200) == 200
    assert _bucket_over_budget_pct(500) == 200


# ──────────────────────────────────────────────────────────────────────
# M4 Unit 8 — counter-argument FSM (`_resolve_active_argument` +
# `with_state_updates(key_present_for_active_argument=...)`)
# ──────────────────────────────────────────────────────────────────────


def _state_with_argument(argument: ActiveArgument | None = None) -> SessionState:
    return SessionState(
        problem=_problem(),
        system_prompt_version="v0",
        started_at=datetime.now(UTC),
        active_argument=argument,
    )


def test_argument_key_absent_preserves_prior() -> None:
    """`new_active_argument` key absent → prior unchanged.

    Replay-deterministic: M2/M3-era snapshots had no `new_active_argument`
    key; the resolver must treat that as no-change so old sessions
    replay byte-identically.
    """
    prior = ActiveArgument(topic="consistency", opened_at_ms=100, rounds=1)
    state = _state_with_argument(prior)

    updated = state.with_state_updates(
        {"session_summary_append": "Candidate is on capacity now."},
        key_present_for_active_argument=False,
        now_ms=200,
    )

    assert updated.active_argument is not None
    assert updated.active_argument.topic == "consistency"
    assert updated.active_argument.rounds == 1


def test_argument_explicit_null_closes_prior() -> None:
    """Explicit `null` → close the argument."""
    prior = ActiveArgument(topic="consistency", opened_at_ms=100, rounds=2)
    state = _state_with_argument(prior)

    updated = state.with_state_updates(
        {"new_active_argument": None},
        key_present_for_active_argument=True,
        now_ms=200,
    )
    assert updated.active_argument is None


def test_fresh_argument_opens_at_rounds_one() -> None:
    """No prior + object value → fresh open at rounds=1, opened_at_ms=now."""
    state = _state_with_argument(None)

    updated = state.with_state_updates(
        {
            "new_active_argument": {
                "topic": "consistency",
                "candidate_pushed_back": False,
            }
        },
        key_present_for_active_argument=True,
        now_ms=12_345,
    )
    assert updated.active_argument is not None
    assert updated.active_argument.topic == "consistency"
    assert updated.active_argument.rounds == 1
    assert updated.active_argument.opened_at_ms == 12_345
    assert updated.active_argument.candidate_pushed_back is False


def test_same_topic_increments_rounds_and_preserves_opened_at_ms() -> None:
    prior = ActiveArgument(
        topic="consistency", opened_at_ms=100, rounds=1, candidate_pushed_back=False
    )
    state = _state_with_argument(prior)

    updated = state.with_state_updates(
        {
            "new_active_argument": {
                "topic": "consistency",
                "candidate_pushed_back": True,
            }
        },
        key_present_for_active_argument=True,
        now_ms=300,
    )
    assert updated.active_argument is not None
    assert updated.active_argument.topic == "consistency"
    assert updated.active_argument.rounds == 2
    # opened_at_ms preserved from the original opener.
    assert updated.active_argument.opened_at_ms == 100
    assert updated.active_argument.candidate_pushed_back is True


def test_different_topic_starts_fresh_argument_at_rounds_one() -> None:
    prior = ActiveArgument(topic="consistency", opened_at_ms=100, rounds=2)
    state = _state_with_argument(prior)

    updated = state.with_state_updates(
        {
            "new_active_argument": {
                "topic": "caching_strategy",
                "candidate_pushed_back": False,
            }
        },
        key_present_for_active_argument=True,
        now_ms=500,
    )
    assert updated.active_argument is not None
    assert updated.active_argument.topic == "caching_strategy"
    assert updated.active_argument.rounds == 1
    assert updated.active_argument.opened_at_ms == 500


def test_stale_opener_auto_clears_at_rounds_one() -> None:
    """Prior at rounds=1 older than 3 minutes → cleared even when the brain
    didn't emit `new_active_argument` this turn.

    This is the safety-net branch: covers the brain opening a thread
    the candidate never engages with, then the brain forgets to close it.
    Every fresh-open / topic-change branch in `_resolve_active_argument`
    assigns ``rounds=1``, so ``rounds==1`` is the operational
    "opened-but-never-followed-up" state — the prior ``rounds==0``
    condition was permanently unreachable in production.
    """
    prior = ActiveArgument(topic="consistency", opened_at_ms=0, rounds=1)
    state = _state_with_argument(prior)

    updated = state.with_state_updates(
        {"session_summary_append": "moving on"},
        key_present_for_active_argument=False,
        now_ms=200_000,  # 200 s > 180 s window
    )
    assert updated.active_argument is None


def test_stale_opener_does_not_auto_clear_when_rounds_advanced() -> None:
    """Auto-clear applies ONLY at rounds=1; an active dispute is preserved."""
    prior = ActiveArgument(topic="consistency", opened_at_ms=0, rounds=2)
    state = _state_with_argument(prior)

    updated = state.with_state_updates(
        {"session_summary_append": "still arguing"},
        key_present_for_active_argument=False,
        now_ms=10_000_000,  # very far in the future
    )
    assert updated.active_argument is not None
    assert updated.active_argument.topic == "consistency"


def test_stale_auto_clear_skipped_when_now_ms_omitted() -> None:
    """Replay path (no now_ms) keeps the prior unchanged."""
    prior = ActiveArgument(topic="consistency", opened_at_ms=0, rounds=1)
    state = _state_with_argument(prior)

    updated = state.with_state_updates(
        {"session_summary_append": "replay"},
        key_present_for_active_argument=False,
        now_ms=None,
    )
    assert updated.active_argument is not None


def test_obsolete_consumer_pattern_silently_ignores_explicit_null() -> None:
    """Atomicity regression test (refinements R7).

    The OLD consumer logic was `if updates.get("new_active_argument") is not None: ...`,
    which collapsed absent + explicit-null into the same "no change"
    path. The new resolver requires `key_present` to distinguish them.

    This test pins the load-bearing change: explicit-null with the new
    flag DOES clear the argument; explicit-null WITHOUT the flag (the
    obsolete pattern) would leave it intact. If a future change drops
    the `key_present_for_active_argument` plumbing in the router, this
    assertion will catch the regression on the consumer side.
    """
    prior = ActiveArgument(topic="consistency", opened_at_ms=100, rounds=2)
    state = _state_with_argument(prior)

    # Obsolete pattern — `key_present_for_active_argument=False` even
    # though the brain emitted explicit null. The resolver MUST preserve
    # the prior under this configuration; the test makes the
    # back-compat behaviour explicit.
    obsolete = state.with_state_updates(
        {"new_active_argument": None},
        key_present_for_active_argument=False,
        now_ms=200,
    )
    assert obsolete.active_argument is not None  # would silently leak

    # New consumer pattern — `key_present_for_active_argument=True`
    # correctly clears the argument.
    correct = state.with_state_updates(
        {"new_active_argument": None},
        key_present_for_active_argument=True,
        now_ms=200,
    )
    assert correct.active_argument is None
