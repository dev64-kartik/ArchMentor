"""Concurrent-event coalescer.

If a `canvas_change` arrives while a `turn_end` brain call is queued
(but not yet dispatched), merge them into a single brain call with both
the transcript delta and canvas diff in the event payload.

Implementation lands in M2.
"""

from __future__ import annotations

# Implementation lands in M2.
