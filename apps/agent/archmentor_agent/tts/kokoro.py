"""Kokoro TTS adapter via `streaming-tts` on Apple MPS.

`streaming_tts.KokoroEngine.synthesize(text)` is a *blocking* sync call
that pushes int16 PCM chunks into `engine.queue` as the model
progresses. We run it on a worker thread and bridge chunks to the
caller's event loop through an asyncio.Queue, yielding float32 mono
arrays at Kokoro's native 24 kHz.

`streaming_tts` is part of the optional `audio` extra; imported lazily.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from collections.abc import AsyncIterator
from queue import Empty
from typing import Any

import numpy as np
import structlog

from archmentor_agent.audio.stt import AudioExtrasMissingError
from archmentor_agent.config import get_settings

log = structlog.get_logger(__name__)

KOKORO_SAMPLE_RATE = 24_000
_DRAIN_POLL_S = 0.05

_ENGINE_SINGLETON: Any | None = None
_ENGINE_LOCK = threading.Lock()


def _load_engine() -> Any:
    global _ENGINE_SINGLETON
    if _ENGINE_SINGLETON is not None:
        return _ENGINE_SINGLETON
    with _ENGINE_LOCK:
        if _ENGINE_SINGLETON is not None:
            return _ENGINE_SINGLETON
        try:
            module = importlib.import_module("streaming_tts")
        except ImportError as exc:
            raise AudioExtrasMissingError() from exc
        voice = get_settings().tts_voice
        log.info("kokoro.load", voice=voice, sample_rate=KOKORO_SAMPLE_RATE)
        _ENGINE_SINGLETON = module.KokoroEngine(voice=voice)
        return _ENGINE_SINGLETON


async def synthesize(text: str) -> AsyncIterator[np.ndarray]:
    """Stream audio chunks for `text`.

    Yields float32 mono arrays at 24 kHz. Cancellation via task.cancel
    propagates through the finally block — we can't stop KokoroEngine
    mid-synthesis (its `synthesize` is an atomic call) but we stop
    consuming and let the worker thread drain and exit.
    """
    if not text.strip():
        return
    engine = _load_engine()
    async for chunk in _stream_engine(engine, text):
        yield chunk


async def _stream_engine(engine: Any, text: str) -> AsyncIterator[np.ndarray]:
    loop = asyncio.get_running_loop()
    bridge: asyncio.Queue[np.ndarray | None] = asyncio.Queue()
    synth_error: list[BaseException] = []

    synth_done = threading.Event()
    # Set when the generator exits (either consumer broke out early,
    # the task was cancelled, or the loop is tearing down). The drain
    # thread checks this before every `call_soon_threadsafe` so it
    # doesn't try to post to a dead event loop.
    consumer_gone = threading.Event()

    def _safe_post(item: np.ndarray | None) -> None:
        """Post to the asyncio bridge only if the consumer is still alive.

        `loop.call_soon_threadsafe` raises `RuntimeError: Event loop is
        closed` when the consumer's loop has shut down. That happens
        after a task cancellation + session teardown race.
        """
        if consumer_gone.is_set() or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(_put_nowait, bridge, item)
        except RuntimeError:
            # Loop closed between the check and the call — consumer is
            # gone. Drop the item; the drain thread will exit on its
            # next iteration when it sees `consumer_gone`.
            consumer_gone.set()

    def _run_synth() -> None:
        try:
            engine.synthesize(text)
        except BaseException as exc:
            synth_error.append(exc)
        finally:
            synth_done.set()

    def _drain_queue() -> None:
        # Drain engine.queue into the asyncio bridge until the synth
        # thread is done AND the queue is empty, then signal end-of-stream.
        while not consumer_gone.is_set():
            try:
                chunk: Any = engine.queue.get(timeout=_DRAIN_POLL_S)
            except Empty:
                if synth_done.is_set():
                    _safe_post(None)
                    return
                continue
            if chunk is None:  # producer-side sentinel
                _safe_post(None)
                return
            arr = _int16_bytes_to_float32(chunk)
            _safe_post(arr)

    synth_thread = threading.Thread(target=_run_synth, name="kokoro.synth", daemon=True)
    drain_thread = threading.Thread(target=_drain_queue, name="kokoro.drain", daemon=True)
    synth_thread.start()
    drain_thread.start()

    try:
        while True:
            item = await bridge.get()
            if item is None:
                break
            yield item
    finally:
        # Tell the drain thread to stop posting before we wait on it.
        consumer_gone.set()
        # Ensure workers exit so threads aren't left orphaned. Engine
        # inference is an atomic call we can't interrupt; on cancel we
        # accept the worst-case 2 s stall. The daemon=True flag above
        # ensures the thread can't block interpreter shutdown either.
        synth_thread.join(timeout=2.0)
        drain_thread.join(timeout=2.0)
        if synth_error:
            raise synth_error[0]


def _put_nowait(q: asyncio.Queue[Any], item: Any) -> None:
    q.put_nowait(item)


def _int16_bytes_to_float32(chunk: Any) -> np.ndarray:
    """Convert whatever the engine hands us (bytes | ndarray) to float32 mono."""
    if isinstance(chunk, bytes | bytearray | memoryview):
        int16 = np.frombuffer(chunk, dtype=np.int16)
    else:
        int16 = np.asarray(chunk).reshape(-1)
        if int16.dtype != np.int16:
            # Already float? Normalize.
            if np.issubdtype(int16.dtype, np.floating):
                return int16.astype(np.float32, copy=False).reshape(-1)
            int16 = int16.astype(np.int16)
    return (int16.astype(np.float32) / 32_767.0).astype(np.float32)
