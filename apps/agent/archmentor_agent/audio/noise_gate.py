"""Pre-VAD noise gate.

Filters mechanical sounds (keyboard, trackpad, mouse clicks) before they
reach Silero VAD, preventing false turn-end events and whisper
hallucinations on non-speech input.

Approach:
- Energy threshold (sliding RMS window)
- Spectral filter (reject high-frequency transient bursts)

Implementation lands in M1.
"""

from __future__ import annotations

# Implementation lands in M1.
