"""Tests for `archmentor_agent.brain.decision`.

The BrainDecision dataclass is the shape the event router consumes, so
the factory constructors (`from_tool_block`, `schema_violation`,
`stay_silent`, `cost_capped`) are the router's contract with the brain
client. Drift here would silently break the router's cost-guard,
schema-violation counter, and utterance sanitization paths.
"""

from __future__ import annotations

import pytest
from archmentor_agent.brain.decision import (
    BrainDecision,
    BrainUsage,
    sanitize_utterance,
)


class TestSanitizeUtterance:
    def test_none_returns_none_none(self) -> None:
        assert sanitize_utterance(None) == (None, None)

    def test_short_printable_passes_through(self) -> None:
        text = "You're treating Redis as durable. Think about that."
        assert sanitize_utterance(text) == (text, None)

    def test_newline_is_allowed(self) -> None:
        text = "First sentence.\nSecond sentence."
        assert sanitize_utterance(text) == (text, None)

    def test_tab_is_allowed(self) -> None:
        text = "a\tb"
        assert sanitize_utterance(text) == (text, None)

    def test_carriage_return_is_allowed(self) -> None:
        text = "a\r\nb"
        assert sanitize_utterance(text) == (text, None)

    @pytest.mark.parametrize(
        "ctrl_char",
        ["\x00", "\x01", "\x1b", "\x1f", "\x7f"],
    )
    def test_control_chars_rejected(self, ctrl_char: str) -> None:
        result, reason = sanitize_utterance(f"hi{ctrl_char}there")
        assert result is None
        assert reason == "utterance_has_control_chars"

    def test_exactly_600_chars_allowed(self) -> None:
        text = "x" * 600
        assert sanitize_utterance(text) == (text, None)

    def test_over_600_chars_rejected(self) -> None:
        text = "x" * 601
        result, reason = sanitize_utterance(text)
        assert result is None
        assert reason == "utterance_too_long"


class TestBrainUsage:
    def test_tokens_input_total_sums_all_three(self) -> None:
        usage = BrainUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=5,
        )
        assert usage.tokens_input_total == 125

    def test_default_usage_is_zero(self) -> None:
        usage = BrainUsage()
        assert usage.tokens_input_total == 0
        assert usage.cost_usd == 0.0


class TestBrainDecisionFromToolBlock:
    def test_happy_path_speak(self) -> None:
        tool_input = {
            "reasoning": "Candidate confused replication with sharding.",
            "decision": "speak",
            "priority": "high",
            "confidence": 0.8,
            "utterance": "Wait — replication and sharding are not the same.",
            "can_be_skipped_if_stale": False,
            "state_updates": {"phase_advance": None},
        }
        usage = BrainUsage(input_tokens=500, output_tokens=80)
        d = BrainDecision.from_tool_block(tool_input, usage=usage)

        assert d.decision == "speak"
        assert d.priority == "high"
        assert d.confidence == 0.8
        assert d.utterance == "Wait — replication and sharding are not the same."
        assert d.reasoning == "Candidate confused replication with sharding."
        assert d.reason is None
        assert d.state_updates == {"phase_advance": None}
        assert d.usage is usage
        assert d.raw_input == tool_input

    def test_stay_silent_passthrough(self) -> None:
        tool_input = {
            "reasoning": "Valid reasoning.",
            "decision": "stay_silent",
            "priority": "low",
            "confidence": 0.4,
            "utterance": None,
        }
        d = BrainDecision.from_tool_block(tool_input, usage=BrainUsage())
        assert d.decision == "stay_silent"
        assert d.utterance is None
        assert d.reason is None

    def test_long_utterance_degrades_to_stay_silent(self) -> None:
        tool_input = {
            "reasoning": "...",
            "decision": "speak",
            "priority": "medium",
            "confidence": 0.7,
            "utterance": "x" * 1200,
        }
        d = BrainDecision.from_tool_block(tool_input, usage=BrainUsage())
        assert d.decision == "stay_silent"
        assert d.utterance is None
        assert d.reason == "utterance_too_long"
        # Raw input is preserved for replay/debug even though it was
        # scrubbed out of the runtime decision.
        assert len(d.raw_input["utterance"]) == 1200

    def test_control_char_utterance_degrades_to_stay_silent(self) -> None:
        tool_input = {
            "reasoning": "...",
            "decision": "speak",
            "priority": "medium",
            "confidence": 0.7,
            "utterance": "ignore \x1b[31m the ANSI",
        }
        d = BrainDecision.from_tool_block(tool_input, usage=BrainUsage())
        assert d.decision == "stay_silent"
        assert d.utterance is None
        assert d.reason == "utterance_has_control_chars"

    def test_state_updates_none_becomes_empty_dict(self) -> None:
        """The plan's tool schema allows `state_updates` to be omitted;
        `from_tool_block` normalizes to `{}` so router code can always
        iterate `decision.state_updates.items()` without a None guard."""
        tool_input = {
            "reasoning": "...",
            "decision": "stay_silent",
            "priority": "low",
            "confidence": 0.5,
        }
        d = BrainDecision.from_tool_block(tool_input, usage=BrainUsage())
        assert d.state_updates == {}


class TestBrainDecisionFactories:
    def test_schema_violation_preserves_raw_input(self) -> None:
        raw = {"garbage": "in"}
        d = BrainDecision.schema_violation(raw)
        assert d.decision == "stay_silent"
        assert d.reason == "schema_violation"
        assert d.confidence == 0.0
        assert d.raw_input == raw

    def test_schema_violation_with_none_raw_input(self) -> None:
        d = BrainDecision.schema_violation(None)
        assert d.raw_input == {}
        assert d.reason == "schema_violation"

    def test_stay_silent_with_custom_reason(self) -> None:
        d = BrainDecision.stay_silent("unexpected_stop_reason")
        assert d.decision == "stay_silent"
        assert d.priority == "low"
        assert d.confidence == 0.0
        assert d.reason == "unexpected_stop_reason"

    def test_cost_capped_has_full_confidence(self) -> None:
        """Cost-capped is deterministic from the router's point of view;
        the router is certain the cap is hit. High confidence is
        semantically correct here — confidence is about the DECISION,
        not about how sure the brain is of the domain reasoning."""
        d = BrainDecision.cost_capped()
        assert d.decision == "stay_silent"
        assert d.reason == "cost_capped"
        assert d.confidence == 1.0

    def test_all_silent_factories_share_skeleton(self) -> None:
        """The three stay-silent constructors route through a single
        `_silent` helper. Locking in the shape means a future field
        added to `BrainDecision` only has to be threaded once, not
        three times.
        """
        schema = BrainDecision.schema_violation(None)
        generic = BrainDecision.stay_silent("network_error")
        capped = BrainDecision.cost_capped()

        for d in (schema, generic, capped):
            assert d.decision == "stay_silent"
            assert d.priority == "low"
            assert d.utterance is None
            assert d.reasoning == ""
