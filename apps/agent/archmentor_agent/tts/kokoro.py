"""Kokoro TTS adapter via `streaming-tts` on Apple MPS.

Exposes an async generator that yields raw float32 PCM chunks as the
model synthesizes them. The LiveKit `session.say()` integration picks
chunks up frame-by-frame so first-audio latency stays close to the
model's time-to-first-chunk.

`streaming-tts` is part of the optional `audio` extra; it is imported
lazily so the package works in CI without the native wheel.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import structlog

from archmentor_agent.audio.stt import AudioExtrasMissingError

log = structlog.get_logger(__name__)

_ENGINE_SINGLETON: Any | None = None


def _load_engine() -> Any:
    global _ENGINE_SINGLETON
    if _ENGINE_SINGLETON is not None:
        return _ENGINE_SINGLETON
    try:
        module = importlib.import_module("streaming_tts")
    except ImportError as exc:
        raise AudioExtrasMissingError() from exc
    cfg = module.TTSConfig(
        voice=os.environ.get("ARCHMENTOR_TTS_VOICE", "af_bella"),
        device=os.environ.get("ARCHMENTOR_TTS_DEVICE", "mps"),
    )
    log.info("kokoro.load", voice=cfg.voice, device=cfg.device)
    _ENGINE_SINGLETON = module.KokoroTTS(cfg)
    return _ENGINE_SINGLETON


async def synthesize(text: str) -> AsyncIterator[np.ndarray]:
    """Stream audio chunks for `text`.

    Yields float32 mono arrays. Cancellation (task.cancel) terminates
    the underlying generator cleanly via `aclose`.
    """
    if not text.strip():
        return
    engine = _load_engine()
    inner = engine.stream(text)
    try:
        async for chunk in inner:
            # Normalize whatever the lib hands us to float32 mono.
            arr = np.asarray(chunk, dtype=np.float32).reshape(-1)
            yield arr
    finally:
        aclose = getattr(inner, "aclose", None)
        if aclose is not None:
            await aclose()
