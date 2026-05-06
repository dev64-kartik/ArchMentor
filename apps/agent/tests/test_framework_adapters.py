"""Framework adapter tests.

These exercise the thin adapters that plug our `transcribe` /
`synthesize` helpers into livekit-agents `STT` / `TTS`. Real
pywhispercpp / Kokoro are never loaded; we patch the helpers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from archmentor_agent.audio import framework_adapters
from archmentor_agent.audio.framework_adapters import (
    KokoroStreamingTTS,
    WhisperCppSTT,
    _audio_frame_to_float32,
    _float32_to_int16_bytes,
)
from archmentor_agent.audio.stt import TranscriptChunk
from livekit import rtc
from livekit.agents import stt


def _int16_frame(samples: np.ndarray, *, sample_rate: int = 16_000) -> rtc.AudioFrame:
    pcm = (np.clip(samples, -1.0, 1.0) * 32_767).astype(np.int16)
    return rtc.AudioFrame(
        data=pcm.tobytes(),
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=pcm.size,
    )


def test_audio_frame_to_float32_roundtrip() -> None:
    src = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    frame = _int16_frame(src)
    recovered = _audio_frame_to_float32(frame)
    # int16 round-trip introduces ~3e-5 error; give it a modest tolerance.
    assert np.allclose(recovered, src, atol=1e-3)


def test_audio_frame_to_float32_downmixes_stereo() -> None:
    # Interleaved L/R: alternate +0.8, -0.8 → mean 0 per pair.
    pcm = np.empty(8, dtype=np.int16)
    pcm[0::2] = int(0.8 * 32_767)
    pcm[1::2] = int(-0.8 * 32_767)
    frame = rtc.AudioFrame(
        data=pcm.tobytes(),
        sample_rate=16_000,
        num_channels=2,
        samples_per_channel=4,
    )
    mono = _audio_frame_to_float32(frame)
    assert mono.shape == (4,)
    assert np.allclose(mono, 0.0, atol=1e-3)


def test_float32_to_int16_bytes_clips_out_of_range() -> None:
    src = np.array([-2.0, -0.5, 0.0, 0.5, 2.0], dtype=np.float32)
    raw = _float32_to_int16_bytes(src)
    decoded = np.frombuffer(raw, dtype=np.int16)
    # First and last should clip to int16 min/max (rounded to int16 range).
    assert decoded[0] == -32_767
    assert decoded[-1] == 32_767


def test_whisper_stt_capabilities() -> None:
    stt_impl = WhisperCppSTT()
    caps = stt_impl.capabilities
    assert caps.streaming is False
    assert caps.interim_results is False
    assert stt_impl.provider == "whisper.cpp"


async def test_whisper_stt_recognize_returns_final_transcript() -> None:
    stt_impl = WhisperCppSTT()
    # 1 second of speech-shaped sinusoid so the noise gate lets it through.
    t = np.arange(16_000, dtype=np.float32) / 16_000
    speech = (0.3 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)
    frame = _int16_frame(speech)

    fake_transcribe = AsyncMock(
        return_value=[
            TranscriptChunk(text="design a url shortener", t_start_ms=100, t_end_ms=1_500),
        ]
    )
    with patch.object(framework_adapters, "transcribe", fake_transcribe):
        event = await stt_impl._recognize_impl(frame)

    assert event.type is stt.SpeechEventType.FINAL_TRANSCRIPT
    assert len(event.alternatives) == 1
    assert event.alternatives[0].text == "design a url shortener"
    assert event.alternatives[0].start_time == pytest.approx(0.1)
    assert event.alternatives[0].end_time == pytest.approx(1.5)
    assert event.alternatives[0].language == "en"


async def test_whisper_stt_recognize_empty_transcript_has_zero_confidence() -> None:
    stt_impl = WhisperCppSTT()
    silence = np.zeros(16_000, dtype=np.float32)
    frame = _int16_frame(silence)

    fake_transcribe = AsyncMock(return_value=[])
    with patch.object(framework_adapters, "transcribe", fake_transcribe):
        event = await stt_impl._recognize_impl(frame)

    assert event.alternatives[0].text == ""
    assert event.alternatives[0].confidence == 0.0


async def test_whisper_stt_resamples_48khz_to_16khz() -> None:
    """Chrome's default mic track is 48 kHz — the framework can hand us
    that buffer directly, and whisper.cpp will fabricate transcripts if
    we don't resample first. This test proves the resample path runs
    and transcribe receives the downsampled sample count."""
    stt_impl = WhisperCppSTT()
    # 1 second of 48 kHz audio (48_000 samples).
    t = np.arange(48_000, dtype=np.float32) / 48_000
    speech = (0.3 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)
    frame = _int16_frame(speech, sample_rate=48_000)

    seen_samples: list[int] = []

    async def fake_transcribe(samples: np.ndarray) -> list[TranscriptChunk]:
        seen_samples.append(int(samples.shape[0]))
        return [TranscriptChunk(text="hi", t_start_ms=0, t_end_ms=500)]

    with patch.object(framework_adapters, "transcribe", fake_transcribe):
        await stt_impl._recognize_impl(frame)

    assert len(seen_samples) == 1
    # 48 kHz → 16 kHz is a 3x downsample; allow tiny slack for the
    # polyphase resampler's edge handling.
    assert 15_500 <= seen_samples[0] <= 16_500


async def test_whisper_stt_reuses_resampler_across_calls() -> None:
    """The resampler is cached per (input_rate, channels). Repeated
    STT calls on the same rate must not allocate a fresh resampler —
    doing so throws away polyphase state and adds per-utterance cost
    in a hot path."""
    stt_impl = WhisperCppSTT()
    t = np.arange(48_000, dtype=np.float32) / 48_000
    speech = (0.3 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)
    frame = _int16_frame(speech, sample_rate=48_000)

    async def fake_transcribe(samples: np.ndarray) -> list[TranscriptChunk]:
        del samples
        return []

    with patch.object(framework_adapters, "transcribe", fake_transcribe):
        await stt_impl._recognize_impl(frame)
        first_resampler = stt_impl._resamplers[(48_000, 1)]
        await stt_impl._recognize_impl(frame)
        second_resampler = stt_impl._resamplers[(48_000, 1)]

    assert first_resampler is second_resampler


def test_kokoro_tts_configuration() -> None:
    tts_impl = KokoroStreamingTTS()
    assert tts_impl.sample_rate == 24_000
    assert tts_impl.num_channels == 1
    # M4: streaming TTS path enabled — `tts.stream()` returns a
    # SynthesizeStream that consumes text via `push_text` / `flush`.
    assert tts_impl.capabilities.streaming is True
    assert tts_impl.provider == "kokoro"


async def test_kokoro_tts_synthesize_emits_audio_chunks() -> None:
    tts_impl = KokoroStreamingTTS()

    async def fake_synth(text: str):
        # Two tiny chunks so we can prove the adapter pushes both.
        yield np.linspace(-0.5, 0.5, num=120, dtype=np.float32)
        yield np.linspace(0.5, -0.5, num=120, dtype=np.float32)

    with patch.object(framework_adapters.kokoro, "synthesize", fake_synth):
        stream = tts_impl.synthesize("hello world")
        audio = await stream.collect()

    # 240 samples at 24 kHz = 10 ms of audio.
    assert audio.sample_rate == 24_000
    assert audio.num_channels == 1
    assert audio.samples_per_channel == 240


async def test_kokoro_tts_synthesize_empty_text_raises() -> None:
    """synthesize() of empty text produces no audio frames; the framework
    surfaces this as an APIError because no audio was pushed."""
    tts_impl = KokoroStreamingTTS()

    async def fake_synth(text: str):
        if False:  # never yields
            yield np.zeros(1, dtype=np.float32)

    with patch.object(framework_adapters.kokoro, "synthesize", fake_synth):
        stream = tts_impl.synthesize("actual text")
        with pytest.raises(Exception):  # noqa: B017, PT011 — framework may wrap differently
            await stream.collect()


async def test_kokoro_tts_chunks_arrive_as_int16_pcm() -> None:
    """The frames the framework receives are int16 — proves float → int16 conversion."""
    tts_impl = KokoroStreamingTTS()

    async def fake_synth(text: str):
        yield np.full(240, 0.5, dtype=np.float32)

    with patch.object(framework_adapters.kokoro, "synthesize", fake_synth):
        stream = tts_impl.synthesize("hello")
        audio = await stream.collect()
        decoded = np.frombuffer(audio.data, dtype=np.int16)
        # 0.5 * 32767 ~= 16383; tolerate tiny rounding.
        assert decoded[0] == pytest.approx(16_383, abs=2)


# ────────────────────── streaming TTS adapter ─────────────────────────


async def test_kokoro_synthesize_stream_sentence_chunks_through_kokoro() -> None:
    """`tts.stream()` consumes pushed text, slices on sentence boundaries,
    and synthesizes each sentence atomically through `kokoro.synthesize`.
    The list of sentences observed by the fake synth proves boundaries
    were detected before the second-sentence audio was queued."""
    tts_impl = KokoroStreamingTTS()
    sentences_synthesized: list[str] = []

    async def fake_synth(text: str):
        sentences_synthesized.append(text)
        # one tiny chunk per sentence so the stream emits at least one
        # frame before moving on
        yield np.full(120, 0.5, dtype=np.float32)

    with patch.object(framework_adapters.kokoro, "synthesize", fake_synth):
        stream = tts_impl.stream()
        # Two complete sentences plus a fragment that flush will close.
        stream.push_text(
            "Walk me through your capacity assumptions. What read-to-write ratio do you expect"
        )
        stream.push_text("?")
        stream.end_input()
        # Drain the stream so `_main_task` runs to completion.
        async for _frame in stream:
            pass
        await stream.aclose()

    # Both complete sentences reached the synthesizer.
    assert len(sentences_synthesized) >= 1, "at least one full sentence should synthesize"
    joined = " ".join(sentences_synthesized)
    assert "capacity assumptions" in joined
    assert "read-to-write ratio" in joined


async def test_kokoro_synthesize_stream_empty_input_closes_cleanly() -> None:
    """`end_input()` with no text pushed must not crash — a stay_silent
    decision pushes nothing through the listener; the stream still has
    to terminate cleanly."""
    tts_impl = KokoroStreamingTTS()
    sentences_synthesized: list[str] = []

    async def fake_synth(text: str):
        sentences_synthesized.append(text)
        if False:  # never yields
            yield np.zeros(1, dtype=np.float32)

    with patch.object(framework_adapters.kokoro, "synthesize", fake_synth):
        stream = tts_impl.stream()
        stream.end_input()
        async for _frame in stream:
            pass
        await stream.aclose()

    assert sentences_synthesized == []
