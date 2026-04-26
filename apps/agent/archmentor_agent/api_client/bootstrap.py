"""HTTP client for the control-plane bootstrap route.

Fetches the problem card the agent worker needs at session start from
`GET /sessions/{id}/bootstrap`. Mirrors `snapshots.client.SnapshotClient`
patterns: httpx.AsyncClient, X-Agent-Token auth, one retry on 5xx,
typed error on 4xx (permanent failure).

Usage::

    bootstrap = await fetch_session_bootstrap(
        api_url=settings.api_url,
        agent_token=settings.agent_ingest_token.get_secret_value(),
        session_id=session_id,
    )
    problem = ProblemCard(
        slug=bootstrap.problem_slug,
        version=1,
        title=bootstrap.problem_slug,  # API doesn't expose title yet
        statement_md=bootstrap.statement_md,
        rubric_yaml=bootstrap.rubric_yaml,
    )
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from uuid import UUID

import httpx
import structlog

log = structlog.get_logger(__name__)


class BootstrapFetchError(Exception):
    """Raised when the bootstrap fetch fails permanently.

    Permanent failures include 4xx responses (wrong token, session not
    found, session not active) and repeated 5xx / transport errors after
    the retry budget is exhausted. Callers should treat this as a
    hard failure — falling back to `build_dev_problem_card()` is only
    correct on the dev/seed-session path, not in production.
    """

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        detail = f"Bootstrap fetch failed: {reason}"
        if status_code is not None:
            detail += f" (HTTP {status_code})"
        super().__init__(detail)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class SessionBootstrap:
    """Problem content returned by the control-plane bootstrap route."""

    problem_slug: str
    statement_md: str
    rubric_yaml: str


async def fetch_session_bootstrap(
    *,
    api_url: str,
    agent_token: str,
    session_id: UUID,
    timeout_s: float = 5.0,
    max_retries: int = 1,
) -> SessionBootstrap:
    """Fetch the problem bootstrap for a session from the control plane.

    Raises `BootstrapFetchError` on 4xx (permanent — no retry) or after
    exhausting the retry budget on 5xx / transport errors. Never returns
    None; callers can assume the result is valid on success.

    Args:
        api_url: Base URL of the control-plane API.
        agent_token: X-Agent-Token shared secret.
        session_id: UUID of the session to bootstrap.
        timeout_s: Per-request timeout in seconds.
        max_retries: Number of additional attempts after the first (so
            1 retry = 2 total attempts). Only applied on 5xx/transport.
    """
    path = f"/sessions/{session_id}/bootstrap"
    headers = {"X-Agent-Token": agent_token}
    total_attempts = max_retries + 1

    async with httpx.AsyncClient(base_url=api_url, timeout=timeout_s) as client:
        for attempt in range(total_attempts):
            is_last = attempt == total_attempts - 1
            try:
                response = await client.get(path, headers=headers)
            except httpx.HTTPError as exc:
                log.warning(
                    "bootstrap.transport_error",
                    attempt=attempt,
                    session_id=str(session_id),
                    error=str(exc),
                )
                if is_last:
                    raise BootstrapFetchError(f"transport error: {exc}") from exc
                await _sleep_backoff(attempt)
                continue

            if response.status_code == 200:
                data = response.json()
                return SessionBootstrap(
                    problem_slug=data["problem_slug"],
                    statement_md=data["statement_md"],
                    rubric_yaml=data["rubric_yaml"],
                )

            if 400 <= response.status_code < 500:
                # 4xx: permanent failure — wrong token, session missing, or
                # session not active. No retry; raise immediately so the
                # caller can decide how to handle it (e.g. fall back to dev
                # path on dev sessions, error-out on production sessions).
                log.error(
                    "bootstrap.client_error",
                    session_id=str(session_id),
                    status=response.status_code,
                    body=response.text[:200],
                )
                raise BootstrapFetchError(
                    f"client error: {response.text[:200]}",
                    status_code=response.status_code,
                )

            log.warning(
                "bootstrap.server_error",
                attempt=attempt,
                session_id=str(session_id),
                status=response.status_code,
            )
            if is_last:
                raise BootstrapFetchError(
                    f"server error after {total_attempts} attempts",
                    status_code=response.status_code,
                )
            await _sleep_backoff(attempt)

    # Unreachable; loop always raises or returns on the last attempt.
    raise BootstrapFetchError("exhausted retry budget")


async def _sleep_backoff(attempt: int) -> None:
    # 50ms * 2**attempt + jitter, cap at 2s. Mirrors ledger/snapshot clients.
    delay = min(2.0, 0.05 * (2**attempt) + random.uniform(0.0, 0.05))  # noqa: S311
    await asyncio.sleep(delay)
