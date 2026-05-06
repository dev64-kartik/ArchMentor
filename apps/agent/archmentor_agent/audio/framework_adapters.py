"""livekit-agents STT / TTS adapter classes.

These bridge the pure-Python whisper.cpp and Kokoro helpers in
`audio/stt.py` and `tts/kokoro.py` into the shapes the livekit-agents
AgentSession expects.

Why this lives separately: the framework base classes pull in a lot of
runtime machinery (pydantic models, trace spans, metrics). The core
`transcribe()` and `synthesize()` helpers stay framework-agnostic and
unit-tested on their own; this file is the adapter seam.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import numpy as np
import structlog
from livekit import rtc
from livekit.agents import stt, tokenize, tts
from livekit.agents.language import LanguageCode
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, combine_frames

from archmentor_agent.audio.stt import transcribe
from archmentor_agent.tts import kokoro

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from livekit.agents.tts.tts import AudioEmitter

# Kokoro's default output — 24 kHz mono float32.
_KOKORO_SAMPLE_RATE = 24_000
_KOKORO_NUM_CHANNELS = 1

# whisper.cpp is hard-wired to 16 kHz mono. LiveKit's browser track
# typically arrives at 48 kHz (Chrome) and the framework may hand us
# 24 kHz post-processing; either way we must resample before whisper
# sees it, otherwise whisper interprets every buffer as mumble and
# falls back on its language-model prior (= generic hallucinations
# and direct `initial_prompt` leak).
_WHISPER_SAMPLE_RATE = 16_000


class WhisperCppSTT(stt.STT):
    """Batch STT: whisper.cpp via our `audio.stt.transcribe` helper.

    We advertise `streaming=False`; the framework buffers a full
    VAD-bounded candidate turn and calls `_recognize_impl` once with
    the whole buffer. The audio reaches us after VAD has already
    decided this slice contains speech, so we hand it straight to
    whisper without further gating.

    The repo ships a `NoiseGate` for filtering mechanical transients
    (keyboard clacks, trackpad taps), but its energy-threshold +
    streaming-hysteresis design assumes per-frame pre-VAD invocation.
    Running it here on a single 1+ second post-VAD buffer mis-applies
    both stages — the energy gate zeros out otherwise-valid speech
    when the buffered RMS falls below 0.010, and the spectral check
    is meaningless on a multi-second window. Re-introduce noise
    gating once we can hook it into the framework's audio pipe
    *before* VAD sees the frames.
    """

    def __init__(self) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        # Cache resamplers keyed by (input_rate, channels). Re-allocating
        # on every STT call both wastes buffers and throws away the
        # resampler's internal polyphase state (harmless at the edges of
        # utterances but still adds allocations in the hot path).
        self._resamplers: dict[tuple[int, int], rtc.AudioResampler] = {}

    @property
    def model(self) -> str:
        import os

        return os.environ.get("ARCHMENTOR_WHISPER_MODEL", "large-v3")

    @property
    def provider(self) -> str:
        return "whisper.cpp"

    def preload(self) -> None:
        """Load the whisper.cpp model eagerly (normally called from `prewarm`).

        The framework's per-job init watchdog kills the worker if first
        inference has to load a multi-GB model on cold start. `prewarm`
        runs outside that watchdog; calling `preload` there makes the
        cost explicit and surfaces model-load errors at worker startup
        instead of on the first live utterance.

        Separate from `__init__` so tests can construct the adapter
        without depending on `pywhispercpp` being installed.
        """
        # Deferred import — `stt_core` is the pure helper module.
        from archmentor_agent.audio import stt as stt_core

        stt_core._load_model()

    def _resample_to_whisper_rate(self, frame: rtc.AudioFrame, source_rate: int) -> rtc.AudioFrame:
        key = (source_rate, int(frame.num_channels))
        resampler = self._resamplers.get(key)
        if resampler is None:
            resampler = rtc.AudioResampler(
                input_rate=source_rate,
                output_rate=_WHISPER_SAMPLE_RATE,
                num_channels=key[1],
                quality=rtc.AudioResamplerQuality.HIGH,
            )
            self._resamplers[key] = resampler
        out_frames = resampler.push(frame)
        out_frames.extend(resampler.flush())
        if not out_frames:
            # A non-empty input frame should always produce output. If it
            # doesn't, silently passing the original 24/48 kHz buffer to
            # whisper (which is hard-wired for 16 kHz) produces fabricated
            # transcripts — fail loudly instead.
            raise RuntimeError(
                f"AudioResampler produced no output for {source_rate} Hz frame "
                f"({frame.samples_per_channel} samples, {frame.num_channels} ch)"
            )
        return combine_frames(out_frames)

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        del language, conn_options  # reserved; whisper auto-detects language

        try:
            frame = combine_frames(buffer)
            source_rate = int(frame.sample_rate)
            if source_rate != _WHISPER_SAMPLE_RATE:
                frame = self._resample_to_whisper_rate(frame, source_rate)
            samples = _audio_frame_to_float32(frame)
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
            log.info(
                "stt.recognize.begin",
                duration_s=round(float(frame.duration), 2),
                samples=int(samples.shape[0]),
                source_rate=source_rate,
                rms=round(rms, 4),
            )
            chunks = await transcribe(samples)
        except (RuntimeError, ValueError, ImportError) as exc:
            # A single bad buffer must not kill the AgentSession. The
            # most likely causes here — a wrong-rate resample no-output,
            # malformed int16 data, or `pywhispercpp` not installed —
            # are all well-defined and safe to surface as an empty
            # final transcript so the framework continues processing
            # future buffers.
            log.warning("stt.recognize.error", error=str(exc), error_type=type(exc).__name__)
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[
                    stt.SpeechData(
                        language=LanguageCode("en"),
                        text="",
                        start_time=0.0,
                        end_time=0.0,
                        confidence=0.0,
                    )
                ],
            )

        text = " ".join(c.text for c in chunks).strip()
        log.info(
            "stt.recognize.end",
            duration_s=round(float(frame.duration), 2),
            text=text,
            chunk_count=len(chunks),
        )
        start_s = chunks[0].t_start_ms / 1_000.0 if chunks else 0.0
        end_s = chunks[-1].t_end_ms / 1_000.0 if chunks else float(frame.duration)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(
                    language=LanguageCode("en"),
                    text=text,
                    start_time=start_s,
                    end_time=end_s,
                    confidence=1.0 if text else 0.0,
                )
            ],
        )


class KokoroStreamingTTS(tts.TTS):
    """Kokoro TTS — supports both one-shot and streaming synthesis.

    `synthesize(text)` returns a `ChunkedStream` that pulls float32
    frames from `tts.kokoro.synthesize` and emits them as int16 PCM on
    the framework's audio pipe (used by `session.say(...)` callers).

    `stream()` returns a `_KokoroSynthesizeStream` that consumes text
    via `push_text(...)` / `flush()` / `end_input()`, slices the input
    on sentence boundaries via livekit-agents' `SentenceTokenizer`, and
    synthesizes each sentence in turn — used by M4's streaming brain
    pipe (`BrainClient.decide(utterance_listener=...)`).
    """

    def __init__(self) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=_KOKORO_SAMPLE_RATE,
            num_channels=_KOKORO_NUM_CHANNELS,
        )

    @property
    def model(self) -> str:
        import os

        return os.environ.get("ARCHMENTOR_TTS_VOICE", "af_bella")

    @property
    def provider(self) -> str:
        return "kokoro"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return _KokoroChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> _KokoroSynthesizeStream:
        return _KokoroSynthesizeStream(tts=self, conn_options=conn_options)


class _KokoroChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=uuid.uuid4().hex,
            sample_rate=_KOKORO_SAMPLE_RATE,
            num_channels=_KOKORO_NUM_CHANNELS,
            mime_type="audio/pcm",
        )
        async for chunk in kokoro.synthesize(self.input_text):
            output_emitter.push(_float32_to_int16_bytes(chunk))


class _KokoroSynthesizeStream(tts.SynthesizeStream):
    """Streaming Kokoro adapter for sentence-chunked TTS.

    The stream consumes text from `self._input_ch` (`push_text` /
    `flush` / `end_input` on the parent class), slices on sentence
    boundaries with `livekit.agents.tokenize.basic.SentenceTokenizer`,
    and synthesizes each completed sentence atomically through
    `kokoro.synthesize(...)`. PCM frames push into the framework's
    `AudioEmitter` on the per-sentence segment id.

    LiveKit's `SentenceTokenizer.stream()` (BufferedSentenceStream)
    holds back tokens until either ≥ `min_sentence_len` chars have
    accumulated OR `flush()` is called. `end_input()` flushes the
    tail. We pass the defaults (min_sentence_len=20) — see M4 plan
    Unit 4 deferred-to-implementation note. Tune after dogfood.
    """

    async def _run(self, output_emitter: AudioEmitter) -> None:
        request_id = uuid.uuid4().hex
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=_KOKORO_SAMPLE_RATE,
            num_channels=_KOKORO_NUM_CHANNELS,
            mime_type="audio/pcm",
            stream=True,
        )

        sent_tokenizer_stream = tokenize.basic.SentenceTokenizer().stream()

        async def _feed_sentence_stream() -> None:
            """Pump push_text / flush / end_input from `_input_ch` to the
            sentence tokenizer. Runs as a sibling task so the synthesis
            loop below can iterate sentences as they finish."""
            try:
                async for data in self._input_ch:
                    if isinstance(data, self._FlushSentinel):
                        sent_tokenizer_stream.flush()
                        continue
                    sent_tokenizer_stream.push_text(data)
            finally:
                # `end_input` flushes any tail buffered below min-len so
                # the synthesis loop sees the closing fragment.
                sent_tokenizer_stream.end_input()

        feeder = asyncio.create_task(_feed_sentence_stream(), name="kokoro.feed")
        # Single output segment per stream instance — matches the
        # framework's contract (`SynthesizeStream._num_segments` counts
        # input segments, `AudioEmitter._num_segments` counts output
        # segments; the parent `_main_task` asserts they match). One
        # input call to `push_text` is one logical segment regardless
        # of how many sentences the tokenizer carves out of it.
        segment_started = False
        try:
            async for token_data in sent_tokenizer_stream:
                sentence = token_data.token
                if not sentence.strip():
                    continue
                if not segment_started:
                    output_emitter.start_segment(segment_id=token_data.segment_id)
                    segment_started = True
                try:
                    async for chunk in kokoro.synthesize(sentence):
                        output_emitter.push(_float32_to_int16_bytes(chunk))
                except (RuntimeError, ValueError, OSError):
                    # Don't break the outer loop on a single sentence
                    # failure — the next sentence may still synthesize.
                    # The framework's `_main_task` retry path catches
                    # APIError; for non-API errors we want the partial
                    # segment to close cleanly and the next to start.
                    # Narrow to the failure modes Kokoro / MPS / file-IO
                    # actually raise so unrelated programming errors
                    # (TypeError, AttributeError, NameError) surface
                    # loudly instead of being swallowed mid-session.
                    log.exception("kokoro.synth_error", sentence_preview=sentence[:40])
            if segment_started:
                output_emitter.end_segment()
        finally:
            await feeder


def _audio_frame_to_float32(frame: rtc.AudioFrame) -> np.ndarray:
    """Convert an int16 mono LiveKit frame to a [-1, 1] float32 array."""
    pcm = np.frombuffer(frame.data, dtype=np.int16)
    if frame.num_channels > 1:
        # Downmix: average channels. Interleaved layout, so reshape then mean.
        pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
    return (pcm.astype(np.float32) / 32_768.0).astype(np.float32)


def _float32_to_int16_bytes(samples: np.ndarray) -> bytes:
    """Convert a [-1, 1] float32 array to int16 PCM bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32_767.0).astype(np.int16).tobytes()
