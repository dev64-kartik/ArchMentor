"""Unit tests for the Kokoro TTS adapter."""

from __future__ import annotations

import sys
from unittest.mock import patch

import numpy as np
import pytest
from archmentor_agent.audio.stt import AudioExtrasMissingError
from archmentor_agent.tts import kokoro


@pytest.fixture(autouse=True)
def _reset_engine_singleton() -> None:
    kokoro._ENGINE_SINGLETON = None


async def _collect(async_iter) -> list[np.ndarray]:  # type: ignore[no-untyped-def]
    return [chunk async for chunk in async_iter]


async def test_empty_text_yields_nothing() -> None:
    chunks = await _collect(kokoro.synthesize("   "))
    assert chunks == []


async def test_missing_streaming_tts_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "streaming_tts", None)
    with pytest.raises(AudioExtrasMissingError, match="audio"):
        await _collect(kokoro.synthesize("hello"))


async def test_synthesize_normalizes_chunks_to_float32_mono() -> None:
    class FakeEngine:
        async def stream(self, text: str):
            yield np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float64)
            yield np.array([0.5, 0.6], dtype=np.float32)

    with patch.object(kokoro, "_load_engine", return_value=FakeEngine()):
        chunks = await _collect(kokoro.synthesize("hi"))

    assert len(chunks) == 2
    assert all(c.dtype == np.float32 for c in chunks)
    assert all(c.ndim == 1 for c in chunks)
    # First chunk flattened from 2x2 → 4 samples.
    assert chunks[0].shape == (4,)
    assert chunks[1].shape == (2,)


async def test_synthesize_cancellation_closes_cleanly() -> None:
    """Consumer aborts partway — engine's async-gen should aclose quietly."""
    closed = {"flag": False}

    class FakeEngine:
        async def stream(self, text: str):
            try:
                for _ in range(10):
                    yield np.zeros(16, dtype=np.float32)
            finally:
                closed["flag"] = True

    with patch.object(kokoro, "_load_engine", return_value=FakeEngine()):
        agen = kokoro.synthesize("hi")
        first = await agen.__anext__()
        await agen.aclose()  # ty: ignore[unresolved-attribute]

    assert first.shape == (16,)
    assert closed["flag"] is True
