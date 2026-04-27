"""Prompt builder for the per-session summary compactor (M4 Unit 5).

The compactor's job is narrow: read the dropped transcript turns plus
the existing session summary, and produce a 2-3 sentence rewrite that
preserves architectural decisions, capacity assumptions, and unresolved
questions. The structured ``decisions`` log lives in ``SessionState``
separately and is never compressed (sacred-list invariant); the prompt
explicitly tells the model not to duplicate it.

Kept as a separate module from ``haiku_client.py`` so the prompt strings
have one home and the test for prompt content is one ``import`` away
from the strings under test.
"""

from __future__ import annotations

from archmentor_agent.state.session_state import TranscriptTurn

# 800 chars ≈ 130 spoken words ≈ ~3 sentences. Hard cap because the
# compressed summary is read on every subsequent brain call (it ships
# inside the dynamic ``state_json`` blob); an unbounded summary would
# inflate every prompt's token count linearly with session age — the
# exact growth curve the compactor exists to bound. Truncation is
# applied client-side as belt-and-braces; the prompt asks the model
# to keep under 800 chars.
SUMMARY_MAX_CHARS = 800


SYSTEM_PROMPT = (
    "You are a session summary compactor. Compress the dropped turns "
    "into 2-3 sentences focused on architectural decisions, capacity "
    "assumptions, and unresolved questions. Decisions log is already "
    "preserved separately — do not duplicate. Keep under 800 chars."
)


def build_user_message(*, existing_summary: str, dropped_turns: list[TranscriptTurn]) -> str:
    """Assemble the user-message body for a single compaction call.

    ``dropped_turns`` is the slice of oldest-N transcript turns the
    caller is about to discard from the rolling window. Each turn is
    rendered as ``"<speaker>: <text>"`` so the model can attribute
    statements without an extra parsing layer.

    ``existing_summary`` is the session's current summary text;
    omitting it would force the model to re-summarise the whole
    session from scratch and lose the carefully-compressed prior
    history.
    """
    rendered_turns = "\n".join(f"{turn.speaker}: {turn.text}" for turn in dropped_turns)
    summary_block = existing_summary or "(none yet)"
    return f"# Existing summary\n{summary_block}\n\n# Dropped turns\n{rendered_turns}"


__all__ = ["SUMMARY_MAX_CHARS", "SYSTEM_PROMPT", "build_user_message"]
