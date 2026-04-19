"""Anthropic tool-use schema for brain output.

The brain always emits via `interview_decision`. Free-form text is never
used — tool-use guarantees structured schema compliance and eliminates
mid-session JSON parse failures.
"""

from __future__ import annotations

from typing import Any

INTERVIEW_DECISION_TOOL: dict[str, Any] = {
    "name": "interview_decision",
    "description": (
        "Emit the next interview decision. Call this on every turn. "
        "Reasoning is private (not spoken). Utterance is what the candidate hears."
    ),
    "input_schema": {
        "type": "object",
        "required": ["reasoning", "decision", "priority", "confidence"],
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Private chain-of-thought. Never spoken.",
            },
            "decision": {
                "type": "string",
                "enum": ["speak", "stay_silent", "update_only"],
            },
            "priority": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Abstain from speaking below 0.6.",
            },
            "utterance": {
                "type": ["string", "null"],
                "description": "Spoken text. Null if decision != 'speak'.",
            },
            "can_be_skipped_if_stale": {
                "type": "boolean",
                "default": False,
            },
            "state_updates": {
                "type": "object",
                "properties": {
                    "rubric_coverage_delta": {"type": "object"},
                    "phase_advance": {"type": ["string", "null"]},
                    "new_decision": {"type": ["object", "null"]},
                    "new_active_argument": {"type": ["object", "null"]},
                    "session_summary_append": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
}
