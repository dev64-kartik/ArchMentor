"""Tests for `archmentor_agent.brain.prompt_builder`.

The builder is the surface `scripts/replay.py` shares with the live
client. These tests pin the shape of the Anthropic request so replay
stays byte-identical: no duplicate problem statement, no leaked
`problem`/`system_prompt_version` into the user message, cache marker
is set on the system block.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from archmentor_agent.brain.prompt_builder import (
    build_call_kwargs,
    build_system_block,
    build_user_message,
)
from archmentor_agent.brain.tools import INTERVIEW_DECISION_TOOL
from archmentor_agent.state import SessionState
from archmentor_agent.state.session_state import (
    DesignDecision,
    InterviewPhase,
    ProblemCard,
    TranscriptTurn,
)


def _state(
    *,
    statement: str = "Design a URL shortener.",
    rubric: str = "dimensions: [functional, capacity]",
) -> SessionState:
    return SessionState(
        problem=ProblemCard(
            slug="url-shortener",
            version=1,
            title="Design URL Shortener",
            statement_md=statement,
            rubric_yaml=rubric,
        ),
        system_prompt_version="m2-initial",
        started_at=datetime(2026, 4, 22, 10, 0, tzinfo=UTC),
        elapsed_s=120,
        phase=InterviewPhase.REQUIREMENTS,
        transcript_window=[
            TranscriptTurn(t_ms=1_000, speaker="candidate", text="Let me start."),
        ],
        decisions=[
            DesignDecision(
                t_ms=90_000,
                decision="Use Redis for rate limiting",
                reasoning="Fast, in-memory counters.",
            ),
        ],
    )


class TestBuildSystemBlock:
    def test_system_block_has_cache_control(self) -> None:
        block = build_system_block(_state())
        assert block["type"] == "text"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_system_block_contains_policy_problem_and_rubric(self) -> None:
        block = build_system_block(_state())
        text = block["text"]
        # Policy prompt fingerprint — stable line from system.md.
        assert "[Persona]" in text
        assert "[STT errors]" in text
        # Problem statement and rubric inline.
        assert "Design a URL shortener." in text
        assert "dimensions: [functional, capacity]" in text
        # Title header is emitted so the brain sees what problem it's on.
        assert "Design URL Shortener" in text

    def test_system_block_text_is_str_not_bytes(self) -> None:
        block = build_system_block(_state())
        assert isinstance(block["text"], str)


class TestBuildUserMessage:
    def test_role_is_user(self) -> None:
        msg = build_user_message(_state(), {"type": "turn_end", "text": "hi"})
        assert msg["role"] == "user"

    def test_excludes_problem_and_system_prompt_version(self) -> None:
        """The cached system block carries the problem — the dynamic
        user message must not duplicate it."""
        content = build_user_message(_state(), {})["content"]
        # `content` is a string (single text block), so search it directly.
        assert "Design a URL shortener." not in content
        assert "m2-initial" not in content
        assert "url-shortener" not in content

    def test_includes_decisions_and_transcript(self) -> None:
        content = build_user_message(_state(), {})["content"]
        assert "Use Redis for rate limiting" in content
        assert "Let me start." in content

    def test_event_payload_rendered_as_json(self) -> None:
        event = {"type": "turn_end", "text": "What's the QPS target?", "t_ms": 120_000}
        content = build_user_message(_state(), event)["content"]
        # JSON-encoded (sort_keys=True + non-ASCII preserved) so the
        # brain parses it as structured data, not prose.
        assert '"type": "turn_end"' in content
        assert '"What\'s the QPS target?"' in content
        assert "# Event" in content

    def test_state_json_roundtrips(self) -> None:
        """The state JSON blob must be valid JSON so the brain (and any
        replay harness) can parse it back."""
        content = build_user_message(_state(), {})["content"]
        # Extract the first ```json ... ``` block.
        fence_open = content.index("```json\n") + len("```json\n")
        fence_close = content.index("\n```", fence_open)
        payload = json.loads(content[fence_open:fence_close])
        assert "transcript_window" in payload
        assert "decisions" in payload
        assert "problem" not in payload


class TestBuildCallKwargs:
    def test_includes_all_required_anthropic_fields(self) -> None:
        kwargs = build_call_kwargs(
            _state(),
            {"type": "turn_end"},
            model="claude-opus-4-7",
            tool=INTERVIEW_DECISION_TOOL,
        )
        assert kwargs["model"] == "claude-opus-4-7"
        assert kwargs["max_tokens"] == 1024
        assert isinstance(kwargs["system"], list)
        assert len(kwargs["system"]) == 1
        assert kwargs["tools"][0]["name"] == "interview_decision"
        assert kwargs["tool_choice"] == {
            "type": "tool",
            "name": "interview_decision",
        }
        assert len(kwargs["messages"]) == 1
        assert kwargs["messages"][0]["role"] == "user"

    def test_tool_choice_name_follows_tool_name(self) -> None:
        """If someone renames the tool, `tool_choice.name` must match —
        otherwise Anthropic returns a 400. Pin the invariant here so the
        next maintainer doesn't have to re-discover it."""
        custom_tool: dict[str, object] = {**INTERVIEW_DECISION_TOOL, "name": "custom_name"}
        kwargs = build_call_kwargs(
            _state(),
            {},
            model="claude-opus-4-7",
            tool=custom_tool,
        )
        assert kwargs["tool_choice"]["name"] == "custom_name"

    def test_max_tokens_override(self) -> None:
        kwargs = build_call_kwargs(
            _state(),
            {},
            model="claude-opus-4-7",
            tool=INTERVIEW_DECISION_TOOL,
            max_tokens=2048,
        )
        assert kwargs["max_tokens"] == 2048
