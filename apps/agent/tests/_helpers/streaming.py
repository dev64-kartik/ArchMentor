"""Test seam for `messages.stream(...)` — fake AsyncMessageStream.

The production streaming path enters `async with self._client.messages.stream(**kwargs) as stream:`
and consumes events via `async for event in stream:`. We replace the SDK
manager with a duck-typed fake so tests can inject deterministic event
sequences and trigger errors at every phase of the streaming lifecycle
(handshake, mid-stream, post-stream validation).

The fake mimics two SDK classes in one object:

- `AsyncMessageStreamManager` — the no-await context manager returned by
  `messages.stream(...)`; its `__aenter__` yields the underlying stream.
- `AsyncMessageStream` — the async-iterable that yields parsed events
  and exposes `get_final_message()` / `current_message_snapshot`.

Collapsing both into one class is the simplest test-side shape that
still exercises the production code's `async with ... as stream`
binding. See `apps/agent/tests/test_brain_client.py::TestStreamingPath`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeStreamEvent:
    """One event yielded by the fake stream.

    Only `type` and (for `input_json` events) `snapshot` are read by
    `BrainClient._decide_streaming`; other fields exist for parity with
    the SDK shape so tests reading `event.partial_json` still work.
    """

    type: str
    snapshot: dict[str, Any] | None = None
    partial_json: str = ""


def utterance_deltas(text: str, *, chunks: int = 4) -> list[FakeStreamEvent]:
    """Build a sequence of `input_json` events that incrementally reveal `text`.

    Mirrors how Anthropic streams the `utterance` value: each event's
    `snapshot` is the parsed accumulated `tool_use.input` dict, with the
    `utterance` key growing one chunk at a time. The leading events emit
    progressively longer `utterance` substrings; the final event is the
    full string.
    """
    if chunks < 1:
        raise ValueError("chunks must be >= 1")
    if not text:
        return []
    step = max(1, len(text) // chunks)
    events: list[FakeStreamEvent] = []
    cursor = 0
    while cursor < len(text):
        cursor = min(len(text), cursor + step)
        events.append(
            FakeStreamEvent(
                type="input_json",
                snapshot={"utterance": text[:cursor]},
                partial_json=text[cursor - step : cursor],
            )
        )
    # Ensure the last event always carries the complete string.
    if events and events[-1].snapshot is not None:
        events[-1].snapshot["utterance"] = text
    return events


@dataclass
class FakeAsyncMessageStream:
    """Duck-typed stand-in for `AsyncMessageStream` + its manager.

    Construct with the events to yield and the final `Message` the
    SDK would return after `message_stop`. Optional error knobs let
    tests drive every cell of the streaming-error decision matrix
    (M4 plan §405-422).
    """

    events: Iterable[FakeStreamEvent] = field(default_factory=list)
    final_message: Any = None
    aenter_error: BaseException | None = None
    aiter_error: BaseException | None = None
    aiter_error_after: int = 0
    aexit_error: BaseException | None = None

    async def __aenter__(self) -> FakeAsyncMessageStream:
        if self.aenter_error is not None:
            raise self.aenter_error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        if self.aexit_error is not None:
            raise self.aexit_error
        return None

    async def __aiter__(self):
        emitted = 0
        for event in self.events:
            if self.aiter_error is not None and emitted >= self.aiter_error_after:
                raise self.aiter_error
            yield event
            emitted += 1
        # Edge case: error injected after all events consumed.
        if self.aiter_error is not None and emitted >= self.aiter_error_after:
            raise self.aiter_error

    async def get_final_message(self) -> Any:
        return self.final_message

    @property
    def current_message_snapshot(self) -> Any:
        return self.final_message


__all__ = [
    "FakeAsyncMessageStream",
    "FakeStreamEvent",
    "utterance_deltas",
]
