"""Anthropic tool-use schema for brain output.

The brain always emits via `interview_decision`. Free-form text is never
used — tool-use guarantees structured schema compliance and eliminates
mid-session JSON parse failures.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ToolInputSchema(TypedDict):
    type: str
    required: list[str]
    properties: dict[str, Any]
    additionalProperties: bool


class ToolDescriptor(TypedDict):
    name: str
    description: str
    input_schema: ToolInputSchema


# The `priority` enum below mirrors `archmentor_api.models.interruption.InterruptionPriority`
# (StrEnum with "high" | "medium" | "low" values). Keep the two in sync when
# either definition changes — there is no shared import path between the
# agent worker and the API package.
INTERVIEW_DECISION_TOOL: ToolDescriptor = {
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
                    "rubric_coverage_delta": {
                        "type": "object",
                        "description": (
                            "Per-dimension coverage update. Keys are rubric "
                            "dimension names (snake_case); values are objects "
                            "with `covered` (bool), `depth` (one of 'none', "
                            "'shallow', 'solid', 'thorough'), and optional "
                            "`last_touched_t_ms`. As a shorthand you may also "
                            "emit a bare depth string (e.g. 'shallow') and the "
                            "agent will inflate it to the full shape."
                        ),
                        "additionalProperties": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "covered": {"type": "boolean"},
                                        "depth": {
                                            "type": "string",
                                            "enum": [
                                                "none",
                                                "shallow",
                                                "solid",
                                                "thorough",
                                            ],
                                        },
                                        "last_touched_t_ms": {"type": ["integer", "null"]},
                                    },
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "string",
                                    "enum": [
                                        "none",
                                        "shallow",
                                        "solid",
                                        "thorough",
                                    ],
                                },
                            ]
                        },
                    },
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
