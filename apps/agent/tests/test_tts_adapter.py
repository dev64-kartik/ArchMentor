"""Unit tests for the Kokoro TTS adapter."""

from __future__ import annotations

import sys
from queue import Queue
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


class _FakeEngine:
    """Mimics streaming_tts.KokoroEngine: sync `synthesize(text)` that
    pushes int16 bytes into `self.queue`."""

    def __init__(self, chunks_f32: list[np.ndarray]) -> None:
        self.queue: Queue = Queue()
        self._chunks_f32 = chunks_f32

    def synthesize(self, text: str) -> bool:
        for chunk in self._chunks_f32:
            int16 = (np.clip(chunk, -1.0, 1.0) * 32_767).astype(np.int16)
            self.queue.put(int16.tobytes())
        return True


async def test_empty_text_yields_nothing() -> None:
    chunks = await _collect(kokoro.synthesize("   "))
    assert chunks == []


async def test_missing_streaming_tts_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "streaming_tts", None)
    with pytest.raises(AudioExtrasMissingError, match="audio"):
        await _collect(kokoro.synthesize("hello"))


async def test_synthesize_yields_float32_mono_chunks() -> None:
    chunk1 = np.linspace(-0.3, 0.3, num=120, dtype=np.float32)
    chunk2 = np.linspace(0.3, -0.3, num=120, dtype=np.float32)
    engine = _FakeEngine([chunk1, chunk2])

    with patch.object(kokoro, "_load_engine", return_value=engine):
        chunks = await _collect(kokoro.synthesize("hi"))

    assert len(chunks) == 2
    assert all(c.dtype == np.float32 for c in chunks)
    assert all(c.ndim == 1 for c in chunks)
    # int16 round-trip introduces ~3e-5 error; tolerate.
    assert np.allclose(chunks[0], chunk1, atol=1e-3)
    assert np.allclose(chunks[1], chunk2, atol=1e-3)


async def test_synthesize_forwards_engine_errors() -> None:
    class _ErroringEngine:
        queue: Queue = Queue()

        def synthesize(self, text: str) -> bool:
            raise RuntimeError("model failed to load")

    with (
        patch.object(kokoro, "_load_engine", return_value=_ErroringEngine()),
        pytest.raises(RuntimeError, match="model failed to load"),
    ):
        await _collect(kokoro.synthesize("hello"))


async def test_synthesize_cancellation_lets_workers_drain() -> None:
    """Caller stops consuming — background threads must exit cleanly."""
    engine = _FakeEngine([np.zeros(16, dtype=np.float32) for _ in range(3)])

    with patch.object(kokoro, "_load_engine", return_value=engine):
        agen = kokoro.synthesize("hi")
        first = await agen.__anext__()
        await agen.aclose()  # ty: ignore[unresolved-attribute]

    assert first.shape == (16,)
