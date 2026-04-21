"""whisper.cpp STT adapter (Metal backend via pywhispercpp).

We use whisper.cpp — not faster-whisper — because faster-whisper has no
GPU path on Apple Silicon. pywhispercpp exposes the same C++ inference
kernels Metal accelerates.

`pywhispercpp` is an optional dependency (extra `audio`). It is
imported lazily so the rest of the agent package imports and runs
(e.g., in CI) without the native wheel available.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

_MODEL_SINGLETON: Any | None = None


@dataclass(frozen=True)
class TranscriptChunk:
    """A contiguous run of transcribed speech from one STT call."""

    text: str
    t_start_ms: int
    t_end_ms: int


class AudioExtrasMissingError(RuntimeError):
    """Raised when pywhispercpp isn't installed.

    Keep the message actionable — devs see this when they've cloned the
    repo but skipped `uv sync --all-packages --extra audio`.
    """

    def __init__(self) -> None:
        super().__init__(
            "pywhispercpp is not installed. Install the agent audio extras: "
            "`uv sync --all-packages --extra audio` (macOS only)."
        )


def _load_model() -> Any:
    global _MODEL_SINGLETON
    if _MODEL_SINGLETON is not None:
        return _MODEL_SINGLETON

    try:
        module = importlib.import_module("pywhispercpp.model")
    except ImportError as exc:
        raise AudioExtrasMissingError() from exc

    model_name = os.environ.get("ARCHMENTOR_WHISPER_MODEL", "large-v3")
    # pywhispercpp defaults to `~/Library/Application Support/pywhispercpp`
    # which the Claude sandbox denies writes to. Route the model cache
    # to the repo-local `.model-cache/whisper/` (sandbox-writable).
    models_dir = os.environ.get("ARCHMENTOR_WHISPER_DIR", ".model-cache/whisper")
    from pathlib import Path

    Path(models_dir).mkdir(parents=True, exist_ok=True)
    log.info("whisper.cpp.load", model=model_name, models_dir=models_dir)
    _MODEL_SINGLETON = module.Model(model_name, models_dir=models_dir)
    return _MODEL_SINGLETON


async def transcribe(
    samples: np.ndarray,
    *,
    t_offset_ms: int = 0,
) -> list[TranscriptChunk]:
    """Transcribe a mono float32 PCM buffer.

    Args:
        samples: 1-D float32 array in [-1, 1] at 16 kHz.
        t_offset_ms: Session-relative offset so returned timestamps are
            absolute to session start.

    Returns:
        One `TranscriptChunk` per whisper segment.
    """
    if samples.ndim != 1:
        raise ValueError("transcribe expects a 1-D mono buffer")

    loop = asyncio.get_running_loop()
    segments = await loop.run_in_executor(None, _run_inference, samples)
    return [
        TranscriptChunk(
            text=seg["text"].strip(),
            t_start_ms=t_offset_ms + int(seg["t0"] * 10),
            t_end_ms=t_offset_ms + int(seg["t1"] * 10),
        )
        for seg in segments
        if seg["text"].strip()
    ]


_WHISPER_INITIAL_PROMPT = (
    # Keep the prompt minimal. Whisper's initial_prompt is capped at
    # 224 tokens and acts as a decoder prior, not a vocabulary list;
    # enumerating technical terms is whack-a-mole and can miscorrect
    # legitimate utterances elsewhere. The real disambiguation happens
    # downstream in the brain, where Claude has the full session
    # context ("lasting first out" after 3 min of cache discussion is
    # obviously LIFO). This prompt just establishes English + register.
    "System design interview in English with an Indian accent. "
    "Technical discussion of distributed systems, databases, and APIs."
)


# Below this RMS (measured before normalization) we assume the
# buffer is near-silent — Silero VAD occasionally forwards
# breath/room-noise segments, and running whisper on them wastes
# a GPU second to produce a hallucinated "Thank you." / "Right."
# Return empty from `transcribe` so the agent drops the turn.
_MIN_SPEECH_RMS = 0.015

# Target RMS after normalization. Whisper was trained on audio in
# roughly this energy range; pushing quiet input up to here shrinks
# the gap between acoustic evidence and language-model prior.
_NORMALIZE_TARGET_RMS = 0.15

# Gain ceiling. Without this, a truly silent buffer (RMS ≈ 0.001)
# would be amplified 150x and turn room noise into "speech".
_MAX_NORMALIZE_GAIN = 15.0


def _run_inference(samples: np.ndarray) -> list[dict[str, Any]]:
    # LiveKit's default mic pipeline + a MacBook built-in mic
    # routinely hand us buffers at RMS 0.04-0.06 (~5% of full scale).
    # Whisper large-v3-turbo was trained on audio ~3x hotter; on
    # quiet input its language-model prior dominates and produces
    # generic filler ("Right.", "Thank you.", "How's the weather?").
    # RMS-based normalization (not peak) because a single pop/click
    # can dominate the peak while leaving the bulk of the signal
    # near-silent. Gain is capped so pure noise doesn't get scaled
    # into fake speech.
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    if rms < _MIN_SPEECH_RMS:
        log.info("whisper.skip_low_rms", rms=round(rms, 4), samples=int(samples.size))
        return []
    gain = min(_NORMALIZE_TARGET_RMS / rms, _MAX_NORMALIZE_GAIN)
    samples = np.clip(samples * gain, -1.0, 1.0).astype(np.float32, copy=False)

    model = _load_model()

    raw = model.transcribe(
        samples,
        # Pin language. Multilingual models auto-detect on every buffer
        # and on short/quiet turns sometimes mis-identify → garbage.
        language="en",
        # Domain + accent hint. Whisper trained heavily on YouTube;
        # short or ambiguous inputs tend to collapse to generic
        # filler ("thanks for watching", "I'll see you in the next
        # video"). A domain prompt anchors the LM prior.
        initial_prompt=_WHISPER_INITIAL_PROMPT,
        # Strict greedy decoding. The default sampling fallback
        # (retry with higher temperature on low-confidence segments)
        # is a primary source of creative hallucinations.
        temperature=0.0,
        temperature_inc=0.0,
        # More aggressive no-speech detection. Default 0.6 still lets
        # room-noise segments through as hallucinated filler; 0.8
        # rejects them as silence.
        no_speech_thold=0.8,
    )
    return [{"text": s.text, "t0": s.t0, "t1": s.t1} for s in raw]
