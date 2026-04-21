"""Noise gate unit tests with synthetic signals.

We exercise the gate without real audio fixtures: sinusoids stand in
for speech (band-limited, mid-frequency energy), very-short transient
high-frequency bursts for keyboard/trackpad clicks, and zero signal
for silence. The gate is pure numpy; no Metal or mic required.
"""

from __future__ import annotations

import numpy as np
import pytest
from archmentor_agent.audio.noise_gate import NoiseGate, NoiseGateConfig


def _speech_like_frame(
    *,
    duration_ms: int = 20,
    sample_rate: int = 16_000,
    amplitude: float = 0.3,
) -> np.ndarray:
    """Band-limited sinusoids around 300-1500 Hz — mimics voiced speech."""
    n = int(sample_rate * duration_ms / 1_000)
    t = np.arange(n, dtype=np.float32) / sample_rate
    signal = (
        np.sin(2 * np.pi * 300 * t)
        + 0.7 * np.sin(2 * np.pi * 800 * t)
        + 0.4 * np.sin(2 * np.pi * 1_500 * t)
    ).astype(np.float32)
    signal *= amplitude / np.max(np.abs(signal))
    return signal.astype(np.float32)


def _high_freq_transient_frame(
    *,
    duration_ms: int = 20,
    sample_rate: int = 16_000,
    amplitude: float = 0.4,
) -> np.ndarray:
    """Short burst concentrated at 6-7 kHz — mimics a keyboard tap."""
    n = int(sample_rate * duration_ms / 1_000)
    t = np.arange(n, dtype=np.float32) / sample_rate
    signal = (np.sin(2 * np.pi * 6_200 * t) + 0.8 * np.sin(2 * np.pi * 7_400 * t)).astype(
        np.float32
    )
    # Sharp decay envelope so total energy is transient-shaped.
    env = np.exp(-np.arange(n) / (n / 4)).astype(np.float32)
    return (signal * env * amplitude).astype(np.float32)


def _silence_frame(duration_ms: int = 20, sample_rate: int = 16_000) -> np.ndarray:
    return np.zeros(int(sample_rate * duration_ms / 1_000), dtype=np.float32)


def test_silence_is_gated_to_zeros() -> None:
    gate = NoiseGate()
    out = gate.process(_silence_frame())
    assert np.all(out == 0.0)


def test_speech_passes_through() -> None:
    gate = NoiseGate()
    speech = _speech_like_frame()
    out = gate.process(speech)
    # Speech should not be zeroed — at least half the energy must survive.
    assert np.sum(out**2) > 0.5 * np.sum(speech**2)


def test_high_frequency_transient_is_blocked() -> None:
    gate = NoiseGate()
    out = gate.process(_high_freq_transient_frame())
    assert np.all(out == 0.0)


def test_soft_speech_above_threshold_still_passes() -> None:
    gate = NoiseGate()
    # Small but speech-shaped — above energy floor.
    quiet_speech = _speech_like_frame(amplitude=0.05)
    out = gate.process(quiet_speech)
    assert np.any(np.abs(out) > 0)


def test_release_window_preserves_mid_word_dips() -> None:
    """Once speech is confirmed, a following low-energy frame still passes."""
    gate = NoiseGate(NoiseGateConfig(speech_release_ms=100))
    # Frame 1: real speech — opens the gate.
    _ = gate.process(_speech_like_frame(amplitude=0.3))
    # Frame 2: quiet but not silent (below threshold) — would be zeroed
    # without hysteresis.
    quiet = _speech_like_frame(amplitude=0.002)
    out = gate.process(quiet)
    # Either it's preserved (non-zero) or it's zeroed. With release we
    # expect preserved.
    assert np.any(np.abs(out) > 0)


def test_sub_threshold_noise_without_prior_speech_is_gated() -> None:
    gate = NoiseGate()
    # Very low-amplitude noise, nothing to open the gate first.
    out = gate.process(_speech_like_frame(amplitude=0.002))
    assert np.all(out == 0.0)


def test_non_1d_input_raises() -> None:
    gate = NoiseGate()
    with pytest.raises(ValueError, match="1-D mono"):
        gate.process(np.zeros((2, 16), dtype=np.float32))
