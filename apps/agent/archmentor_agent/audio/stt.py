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
    log.info("whisper.cpp.load", model=model_name)
    _MODEL_SINGLETON = module.Model(model_name)
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


def _run_inference(samples: np.ndarray) -> list[dict[str, Any]]:
    model = _load_model()
    raw = model.transcribe(samples)
    return [{"text": s.text, "t0": s.t0, "t1": s.t1} for s in raw]
