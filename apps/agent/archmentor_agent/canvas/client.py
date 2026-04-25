"""HTTP client for `POST /sessions/{id}/canvas-snapshots`.

Mirrors `archmentor_agent.snapshots.client.SnapshotClient` deliberately
— same retry/backoff shape, same auth, same fire-and-forget semantics
— so the two clients stay structurally parallel. If one gains a knob,
the other should too.

The agent fires canvas snapshot writes from `MentorAgent._canvas_tasks`
(lands in Unit 9); the entrypoint drains the set before the client's
`aclose()` so no "client has been closed" errors appear at shutdown.
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
class CanvasSnapshotClientConfig:
    base_url: str
    agent_token: str
    timeout_s: float = 5.0
    max_retries: int = 2  # 2 retries = 3 total attempts


class CanvasSnapshotClient:
    """POST /sessions/{id}/canvas-snapshots — fire-and-forget."""

    def __init__(
        self,
        config: CanvasSnapshotClientConfig,
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
        scene_json: dict[str, Any],
    ) -> bool:
        """Post a canvas snapshot. Returns True on success, False on drop.

        Never raises — callers in the canvas-handler hot path rely on
        non-blocking behavior. A 4xx (e.g. 409 session ended, 422 schema
        violation) logs `client_error` and returns False without
        retrying. A repeated 5xx or transport error logs
        `dropped_after_retries` after the final attempt.
        """
        body = {"t_ms": t_ms, "scene_json": scene_json}
        path = f"/sessions/{session_id}/canvas-snapshots"
        total_attempts = self._cfg.max_retries + 1

        for attempt in range(total_attempts):
            is_last = attempt == total_attempts - 1
            try:
                response = await self._client.post(path, json=body)
            except httpx.HTTPError as exc:
                log.warning("canvas_snapshots.transport_error", attempt=attempt, error=str(exc))
                if is_last:
                    break
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 201:
                return True

            if 400 <= response.status_code < 500:
                log.error(
                    "canvas_snapshots.client_error",
                    status=response.status_code,
                    body=response.text,
                    t_ms=t_ms,
                )
                return False

            log.warning(
                "canvas_snapshots.server_error", attempt=attempt, status=response.status_code
            )
            if is_last:
                break
            await self._sleep_backoff(attempt)

        log.error("canvas_snapshots.dropped_after_retries", t_ms=t_ms)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _sleep_backoff(self, attempt: int) -> None:
        # Match SnapshotClient: 50ms * 2**attempt + jitter, cap at 2s so
        # retry storms don't starve the asyncio loop during recovery.
        delay = min(2.0, 0.05 * (2**attempt) + random.uniform(0.0, 0.05))  # noqa: S311
        await asyncio.sleep(delay)
