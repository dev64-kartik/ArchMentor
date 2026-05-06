"""Scripted `BrainClient` substitute for router and entrypoint tests.

The real client wraps `AsyncAnthropic`; tests don't need that — they
need to drive specific decision sequences and exercise cancellation
mid-`decide`. `FakeBrainClient` returns whatever the test queued and
records the call arguments for later assertion.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from archmentor_agent.brain.decision import BrainDecision, BrainUsage
from archmentor_agent.state.session_state import SessionState

DecisionFactory = Callable[[SessionState, dict[str, Any], int], BrainDecision]


@dataclass
class _RecordedCall:
    state: SessionState
    event: dict[str, Any]
    t_ms: int


@dataclass
class FakeBrainClient:
    """Returns scripted decisions; records each call.

    Two ways to script:
    - `enqueue(decision)` adds a single decision to the FIFO.
    - `decision_factory` is invoked when the FIFO is empty so tests
      can compute a decision from the call arguments. Defaults to
      `BrainDecision.stay_silent("default_test_response")`.
    """

    decision_factory: DecisionFactory | None = None
    delay_s: float = 0.0
    raise_on_call: BaseException | None = None
    calls: list[_RecordedCall] = field(default_factory=list)
    _scripted: deque[BrainDecision] = field(default_factory=deque)

    def enqueue(self, decision: BrainDecision) -> None:
        self._scripted.append(decision)

    def enqueue_speak(
        self,
        utterance: str,
        *,
        confidence: float = 0.9,
        cost_usd: float = 0.0,
    ) -> None:
        self._scripted.append(
            BrainDecision(
                decision="speak",
                priority="medium",
                confidence=confidence,
                reasoning="test reasoning",
                utterance=utterance,
                usage=BrainUsage(input_tokens=10, output_tokens=5, cost_usd=cost_usd),
            )
        )

    def enqueue_stay_silent(
        self,
        reason: str = "test",
        *,
        cost_usd: float = 0.0,
    ) -> None:
        self._scripted.append(
            BrainDecision(
                decision="stay_silent",
                priority="low",
                confidence=0.0,
                reasoning="",
                reason=reason,
                usage=BrainUsage(cost_usd=cost_usd),
            )
        )

    def enqueue_schema_violation(self) -> None:
        self._scripted.append(BrainDecision.schema_violation(None))

    async def decide(
        self,
        *,
        state: SessionState,
        event: dict[str, Any],
        t_ms: int,
        utterance_listener: Callable[[str], Any] | None = None,
    ) -> BrainDecision:
        self.calls.append(_RecordedCall(state=state, event=dict(event), t_ms=t_ms))
        # M4: when an `utterance_listener` is wired and the scripted
        # decision is `speak`, push the full utterance through the
        # listener so streaming-tts integration tests observe `audio_played`.
        # Tests that don't care about the listener pass `utterance_listener=None`.
        if self.delay_s > 0:
            # `asyncio.sleep` is a cancellation point — exactly what
            # the cancel-mid-call tests rely on.
            await asyncio.sleep(self.delay_s)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if self._scripted:
            decision = self._scripted.popleft()
        elif self.decision_factory is not None:
            decision = self.decision_factory(state, event, t_ms)
        else:
            decision = BrainDecision.stay_silent("default_test_response")
        if utterance_listener is not None and decision.decision == "speak" and decision.utterance:
            result = utterance_listener(decision.utterance)
            if asyncio.iscoroutine(result):
                await result
        return decision

    async def aclose(self) -> None:
        return None


__all__ = ["FakeBrainClient"]
