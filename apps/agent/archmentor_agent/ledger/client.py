"""HTTP client for the control-plane event ledger.

Writes go through the API so we don't duplicate DB credentials into the
agent worker. Retries on transient 5xx responses; drops on repeated
failure — the event ledger is best-effort from the agent's perspective,
and blocking the voice loop on a flaky API is the wrong tradeoff.
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
class LedgerConfig:
    base_url: str
    agent_token: str
    timeout_s: float = 5.0
    max_retries: int = 2  # 2 retries = 3 total attempts


class LedgerClient:
    """Thin wrapper around httpx.AsyncClient for POST /sessions/{id}/events."""

    def __init__(self, config: LedgerConfig, client: httpx.AsyncClient | None = None) -> None:
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
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Append an event. Returns True on success, False on permanent failure.

        Never raises — callers in the hot path rely on non-blocking
        behavior.
        """
        body = {"t_ms": t_ms, "type": event_type, "payload_json": payload}
        path = f"/sessions/{session_id}/events"

        for attempt in range(self._cfg.max_retries + 1):
            try:
                response = await self._client.post(path, json=body)
            except httpx.HTTPError as exc:
                log.warning("ledger.transport_error", attempt=attempt, error=str(exc))
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 201:
                return True

            # 4xx: client error (bad payload, expired session). No point retrying.
            if 400 <= response.status_code < 500:
                log.error(
                    "ledger.client_error",
                    status=response.status_code,
                    body=response.text,
                    event_type=event_type,
                )
                return False

            # 5xx: transient. Back off and retry.
            log.warning("ledger.server_error", attempt=attempt, status=response.status_code)
            await self._sleep_backoff(attempt)

        log.error("ledger.dropped_after_retries", event_type=event_type, t_ms=t_ms)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _sleep_backoff(self, attempt: int) -> None:
        # 50ms * 2**attempt + jitter. Cap at 2s so we don't starve the loop.
        delay = min(2.0, 0.05 * (2**attempt) + random.uniform(0.0, 0.05))  # noqa: S311
        await asyncio.sleep(delay)
