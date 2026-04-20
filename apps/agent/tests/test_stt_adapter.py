"""Unit tests for the STT adapter.

We never exercise pywhispercpp itself — these tests prove the adapter
contract: shape of the public API, clean error when extras are missing,
and correct mapping from whisper segment output to TranscriptChunk.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from archmentor_agent.audio import stt


@pytest.fixture(autouse=True)
def _reset_model_singleton() -> None:
    stt._MODEL_SINGLETON = None


def test_non_1d_input_raises() -> None:
    import asyncio

    with pytest.raises(ValueError, match="1-D"):
        asyncio.run(stt.transcribe(np.zeros((2, 4), dtype=np.float32)))


def test_missing_pywhispercpp_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate the extra not installed: remove the module from sys.modules
    # AND block the import path.
    monkeypatch.setitem(sys.modules, "pywhispercpp.model", None)
    import asyncio

    with pytest.raises(stt.AudioExtrasMissingError, match="audio"):
        asyncio.run(stt.transcribe(np.zeros(16, dtype=np.float32)))


def test_transcribe_maps_segments_to_chunks() -> None:
    fake_segments = [
        SimpleNamespace(text="  hello there ", t0=100, t1=250),
        SimpleNamespace(text=" ", t0=250, t1=260),  # whitespace-only: filter
        SimpleNamespace(text="world", t0=300, t1=450),
    ]
    fake_model = SimpleNamespace(transcribe=lambda _: fake_segments)

    with patch.object(stt, "_load_model", return_value=fake_model):
        import asyncio

        chunks = asyncio.run(stt.transcribe(np.zeros(16, dtype=np.float32), t_offset_ms=2000))

    # Whitespace-only segment is dropped; t0/t1 are whisper-centiseconds
    # (x10 -> ms), offset by t_offset_ms.
    assert [(c.text, c.t_start_ms, c.t_end_ms) for c in chunks] == [
        ("hello there", 3000, 4500),
        ("world", 5000, 6500),
    ]
