"""HTTP client for the control-plane brain-snapshot ingest route.

Mirrors `archmentor_agent.ledger.client.LedgerClient` deliberately:

- Same retry-with-backoff shape (2 retries on 5xx + transport errors,
  drop on 4xx, all best-effort so the voice loop never blocks).
- Same `X-Agent-Token` shared-secret auth.
- Structural copy rather than a shared base class — the two clients
  target different routes with different payload shapes, and a thin
  base class would leak each client's concerns into the other.

The agent fires snapshot writes from `MentorAgent._snapshot_tasks`
(see plan Unit 7); task drain awaits them before `aclose()` so no
"client has been closed" errors appear in shutdown logs.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SnapshotClientConfig:
    base_url: str
    agent_token: str
    timeout_s: float = 5.0
    max_retries: int = 2  # 2 retries = 3 total attempts


class SnapshotClient:
    """POST /sessions/{id}/snapshots — fire-and-forget."""

    def __init__(
        self,
        config: SnapshotClientConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = config
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_s,
            headers={"X-Agent-Token": config.agent_token},
        )

    async def append(
        self,
        *,
        session_id: UUID,
        t_ms: int,
        session_state_json: dict[str, Any],
        event_payload_json: dict[str, Any],
        brain_output_json: dict[str, Any],
        reasoning_text: str = "",
        tokens_input: int = 0,
        tokens_output: int = 0,
    ) -> bool:
        """Post a snapshot. Returns True on success, False on drop.

        Never raises — callers in the hot path rely on non-blocking
        behavior. A 4xx (e.g. session ended) logs `client_error` and
        returns False without retrying. A repeated 5xx or transport
        error logs `dropped_after_retries` after the final attempt.
        """
        body = {
            "t_ms": t_ms,
            "session_state_json": session_state_json,
            "event_payload_json": event_payload_json,
            "brain_output_json": brain_output_json,
            "reasoning_text": reasoning_text,
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
        }
        path = f"/sessions/{session_id}/snapshots"
        total_attempts = self._cfg.max_retries + 1

        for attempt in range(total_attempts):
            is_last = attempt == total_attempts - 1
            try:
                response = await self._client.post(path, json=body)
            except httpx.HTTPError as exc:
                log.warning("snapshots.transport_error", attempt=attempt, error=str(exc))
                if is_last:
                    break
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 201:
                return True

            if 400 <= response.status_code < 500:
                log.error(
                    "snapshots.client_error",
                    status=response.status_code,
                    body=response.text,
                    t_ms=t_ms,
                )
                return False

            log.warning("snapshots.server_error", attempt=attempt, status=response.status_code)
            if is_last:
                break
            await self._sleep_backoff(attempt)

        log.error("snapshots.dropped_after_retries", t_ms=t_ms)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _sleep_backoff(self, attempt: int) -> None:
        # Match LedgerClient: 50ms * 2**attempt + jitter, cap at 2s so
        # retry storms don't starve the asyncio loop during recovery.
        delay = min(2.0, 0.05 * (2**attempt) + random.uniform(0.0, 0.05))  # noqa: S311
        await asyncio.sleep(delay)
