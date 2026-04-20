"""Pre-VAD noise gate.

Filters mechanical sounds (keyboard clacks, trackpad taps, mouse clicks)
*before* they reach Silero VAD. Two stages run per frame:

1. **Energy gate.** Frames below a sliding-RMS threshold are discarded as
   silence. Prevents VAD from chewing on idle-room hum.
2. **Spectral filter.** Short transient bursts with energy concentrated
   in high frequencies (>4 kHz relative to the broad band) are treated
   as mechanical hits and muted. Human speech energy is concentrated
   below 4 kHz; a desk tap is almost all high-frequency transient.

The gate is stateful so callers can push streaming frames. Call
:func:`NoiseGate.process` with a `float32` mono PCM frame in [-1.0, 1.0]
and a sample rate; it returns the frame with gated samples zeroed out.
Zeroing (not dropping) preserves frame alignment for downstream VAD.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class NoiseGateConfig:
    """Tuning knobs. Defaults chosen for 16 kHz microphone audio."""

    sample_rate: int = 16_000
    # Below this RMS (amplitude 0-1), a frame is treated as silence.
    energy_threshold: float = 0.010
    # Fraction of spectral energy above `high_freq_cutoff_hz` that flags
    # a frame as a mechanical transient. Human speech stays well below
    # this; keyboard/trackpad taps exceed it because their energy is
    # heavily weighted toward the high band.
    high_freq_cutoff_hz: float = 4_000.0
    high_freq_energy_ratio: float = 0.60
    # Speech-confirmation hysteresis: once a frame is confirmed speech,
    # subsequent frames are passed through for this many milliseconds
    # even if they dip below the energy threshold. Prevents mid-word
    # clipping.
    speech_release_ms: int = 250


class NoiseGate:
    """Streaming noise gate. Instance per audio track."""

    def __init__(self, config: NoiseGateConfig | None = None) -> None:
        self._cfg = config or NoiseGateConfig()
        self._release_samples_remaining = 0

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Return `frame` with non-speech content zeroed out.

        Args:
            frame: 1-D float32 array in [-1, 1]. Must be mono.

        Returns:
            Same-shape array; zeros where the gate closed.
        """
        if frame.ndim != 1:
            raise ValueError("NoiseGate expects a 1-D mono frame")
        if frame.dtype != np.float32:
            frame = frame.astype(np.float32, copy=False)

        rms = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
        is_mechanical = self._is_mechanical_transient(frame)
        has_energy = rms >= self._cfg.energy_threshold

        if is_mechanical:
            # Hard-reject transients regardless of prior speech state; a
            # keyboard hit that overlaps speech already ruined that frame
            # for whisper, and Silero is better served by silence than
            # noise.
            self._release_samples_remaining = 0
            return np.zeros_like(frame)

        if has_energy:
            self._release_samples_remaining = int(
                self._cfg.sample_rate * self._cfg.speech_release_ms / 1_000
            )
            return frame

        if self._release_samples_remaining > 0:
            self._release_samples_remaining = max(0, self._release_samples_remaining - frame.size)
            return frame

        return np.zeros_like(frame)

    def _is_mechanical_transient(self, frame: np.ndarray) -> bool:
        """Return True if the frame's energy is dominated by the high band."""
        if frame.size < 32:
            # FFT on very short frames is unreliable; defer to the energy
            # stage alone.
            return False
        spectrum = np.abs(np.fft.rfft(frame)) ** 2
        total = float(spectrum.sum())
        if total <= 0.0:
            return False
        freqs = np.fft.rfftfreq(frame.size, d=1.0 / self._cfg.sample_rate)
        high_band_mask = freqs >= self._cfg.high_freq_cutoff_hz
        high_energy = float(spectrum[high_band_mask].sum())
        return (high_energy / total) >= self._cfg.high_freq_energy_ratio
