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

from typing import TYPE_CHECKING

import numpy as np
from livekit import rtc
from livekit.agents import stt, tts
from livekit.agents.language import LanguageCode
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, combine_frames

from archmentor_agent.audio.noise_gate import NoiseGate
from archmentor_agent.audio.stt import transcribe
from archmentor_agent.tts import kokoro

if TYPE_CHECKING:
    from livekit.agents.tts.tts import AudioEmitter

# Kokoro's default output — 24 kHz mono float32.
_KOKORO_SAMPLE_RATE = 24_000
_KOKORO_NUM_CHANNELS = 1


class WhisperCppSTT(stt.STT):
    """Batch STT: whisper.cpp via our `audio.stt.transcribe` helper.

    We advertise `streaming=False`; the framework will buffer an entire
    candidate turn (VAD-bounded) and call `_recognize_impl` once. Noise
    gating happens here — before audio reaches whisper — so mechanical
    keyboard/trackpad transients are zeroed out of the buffer that
    whisper sees.
    """

    def __init__(self) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._noise_gate = NoiseGate()

    @property
    def model(self) -> str:
        import os

        return os.environ.get("ARCHMENTOR_WHISPER_MODEL", "large-v3")

    @property
    def provider(self) -> str:
        return "whisper.cpp"

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        del language, conn_options  # reserved; whisper auto-detects language

        frame = combine_frames(buffer)
        samples = _audio_frame_to_float32(frame)
        gated = self._noise_gate.process(samples)
        chunks = await transcribe(gated)

        text = " ".join(c.text for c in chunks).strip()
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
    """Kokoro streaming TTS.

    `synthesize(text)` returns a `ChunkedStream` that pulls float32
    frames from `tts.kokoro.synthesize` and emits them as int16 PCM on
    the framework's audio pipe.
    """

    def __init__(self) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
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


class _KokoroChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: AudioEmitter) -> None:
        import uuid

        output_emitter.initialize(
            request_id=uuid.uuid4().hex,
            sample_rate=_KOKORO_SAMPLE_RATE,
            num_channels=_KOKORO_NUM_CHANNELS,
            mime_type="audio/pcm",
        )
        async for chunk in kokoro.synthesize(self.input_text):
            output_emitter.push(_float32_to_int16_bytes(chunk))


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
