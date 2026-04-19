"""Atomic SessionState I/O against Redis.

Key layout
----------
    session:{id}:state            → JSON-serialized SessionState

No TTL on session keys — explicit cleanup on session end (or via the
stale-session reaper introduced in M6). This prevents state eviction
during pauses or bathroom breaks.

M2 will add a Lua script for atomic read-modify-write across the hot
path (decisions append, rubric delta, summary append).
"""

from __future__ import annotations

# Implementation lands in M2.
