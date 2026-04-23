"""Tests for `_is_whisper_hallucination`.

Whisper hallucinates stock YouTube filler on near-silent or noisy
buffers. The filter is the last line of defence between the STT path
and the ledger — a regression that either lets every hallucination
through or drops legitimate short answers is high-impact and silent.
"""

from __future__ import annotations

import pytest
from archmentor_agent.main import _is_whisper_hallucination


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\t",
    ],
)
def test_empty_strings_are_hallucinations(text: str) -> None:
    assert _is_whisper_hallucination(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "[Music]",
        "[BLANK_AUDIO]",
        "[Silence]",
        "[Noise]",
        "(music)",
        "(silence)",
        "(Blank_Audio)",
    ],
)
def test_bracketed_sound_tags_are_hallucinations(text: str) -> None:
    assert _is_whisper_hallucination(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "[anything in brackets]",
        "[foo bar baz]",
    ],
)
def test_any_fully_bracketed_text_is_hallucination(text: str) -> None:
    """The filter treats any `[...]`-wrapped output as a whisper tag.

    Whisper has a long tail of bracketed event labels beyond the ones
    we enumerate; rather than chase them, the filter drops anything
    that's purely a bracket-wrapped fragment.
    """
    assert _is_whisper_hallucination(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "thanks for watching",
        "Thanks for watching.",
        "Thanks for watching!",
        "THANK YOU FOR WATCHING",
        "see you in the next video",
        "see you next time",
        "subscribe to my channel",
        "and the one that i love",
        "we'll see you next time",
        "this video is sponsored by Squarespace",
        "Don't forget to like!",
        "please subscribe",
        "Bye bye.",
    ],
)
def test_known_youtube_filler_is_hallucination(text: str) -> None:
    assert _is_whisper_hallucination(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "LIFO",
        "yes",
        "no",
        "I would use a hash map here",
        "partitioning by user_id makes more sense",
        "let me think about it",
        "we can batch those writes to a queue",
        "ok",
        "right",
    ],
)
def test_legitimate_technical_utterances_are_not_hallucinations(text: str) -> None:
    """The filter must not drop short technical answers or acknowledgements.

    "yes" / "right" / "ok" are semantically real turns; we rely on the
    upstream RMS gate and Silero VAD to reject truly-silent buffers,
    not this filter.
    """
    assert _is_whisper_hallucination(text) is False


def test_partial_phrase_inside_real_answer_still_flags() -> None:
    """Known-filler phrases match as substrings — a rare but accepted cost.

    A real answer that embeds a hallucination-stem (e.g., someone
    literally saying "thanks for watching" in an interview) would be
    dropped. The filter's policy is: optimize for dropping whisper's
    noise output, accept occasional false positives on off-topic
    pleasantries.
    """
    assert _is_whisper_hallucination("thanks for watching this talk") is True


@pytest.mark.parametrize(
    "text",
    [
        # Exact leak observed 2026-04-23 on a 2.42s, RMS=0.06 buffer.
        "Discussion may switch between English and romanized Hindi�",
        # Other sentence stems of `_WHISPER_INITIAL_PROMPT`.
        "System design interview with an Indian engineer",
        "Technical discussion of distributed systems, databases, and APIs",
        # Mixed case + trailing garbage still flagged.
        "DISCUSSION MAY SWITCH between English and romanized hindi and stuff",
    ],
)
def test_initial_prompt_echo_is_hallucination(text: str) -> None:
    """Whisper echoes `initial_prompt` on short/quiet buffers — drop it."""
    assert _is_whisper_hallucination(text) is True
