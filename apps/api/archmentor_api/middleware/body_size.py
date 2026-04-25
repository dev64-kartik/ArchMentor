"""Body-size limit middleware for agent ingest routes.

Two caps land on the same middleware so a single component holds the
contract for "what fits through the agent → API surface":

- 16 KiB on `/sessions/{id}/events` — the per-event ledger row.
- 256 KiB on `/sessions/{id}/snapshots` and
  `/sessions/{id}/canvas-snapshots` — the brain snapshot row and the
  full Excalidraw scene snapshot.

The middleware rejects with 413 *before* Pydantic deserialization so
multi-megabyte JSON DoS attempts never reach the parser. The in-handler
caps in `routes/sessions.py` stay as defense-in-depth: middleware is the
primary gate, the handler check is the secondary backstop in case the
middleware is misconfigured or bypassed by a future router refactor.

For requests with a `Content-Length` header the rejection is immediate
(no body read). For chunked requests without `Content-Length` the
middleware buffers the body in chunks, counts UTF-8 bytes, and aborts
with 413 the moment the cap is exceeded — so the worst-case memory
footprint for a malicious chunked stream is `cap + 1 chunk`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

# ASGI scope is `dict[str, Any]` per the spec; aliasing improves call-site readability.
Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


# 16 KiB — single ledger event payload.
EVENT_BODY_CAP_BYTES = 16 * 1024
# 256 KiB — brain snapshot (full SessionState + reasoning) and full
# Excalidraw scene snapshot.
SNAPSHOT_BODY_CAP_BYTES = 256 * 1024


def _cap_for_path(path: str) -> int | None:
    """Return the configured byte cap for a request path, or None.

    Matches the path *suffix* under `/sessions/` so dynamic UUID segments
    don't need to be parsed. The session router prefix is the only place
    these suffixes appear, so a startswith + endswith check is unambiguous
    and cheap.
    """
    if not path.startswith("/sessions/"):
        return None
    if path.endswith("/events"):
        return EVENT_BODY_CAP_BYTES
    if path.endswith("/snapshots") or path.endswith("/canvas-snapshots"):
        return SNAPSHOT_BODY_CAP_BYTES
    return None


def _content_length(scope: Scope) -> int | None:
    """Return the Content-Length header as an int, or None if missing/invalid."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_413(send: Send, *, cap: int, observed: int | None) -> None:
    """Emit a 413 response with a JSON body matching the FastAPI handler shape."""
    detail = (
        f"request body too large (max {cap} bytes"
        + (f", observed {observed} bytes" if observed is not None else "")
        + ")"
    )
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before the handler runs.

    Plain ASGI middleware (not `BaseHTTPMiddleware`) so we can inspect
    `Content-Length` and short-circuit without ever calling the inner app —
    `BaseHTTPMiddleware` reads the body into memory first, defeating the
    point of a body-size gate.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        cap = _cap_for_path(scope.get("path", ""))
        if cap is None:
            await self._app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None:
            if declared > cap:
                await _send_413(send, cap=cap, observed=declared)
                return
            await self._app(scope, receive, send)
            return

        # No Content-Length: chunked encoding (or a client that omitted
        # the header). Buffer the body while counting bytes, abort if it
        # exceeds the cap, replay the buffered chunks otherwise.
        await self._gate_streamed_body(scope, receive, send, cap=cap)

    async def _gate_streamed_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        cap: int,
    ) -> None:
        chunks: list[Message] = []
        total = 0
        more = True
        while more:
            message = await receive()
            mtype = message.get("type")
            if mtype == "http.disconnect":
                # Client gave up before sending the full body; nothing to
                # forward. Returning without calling the app drops the
                # request silently, which is the right behaviour here.
                return
            if mtype != "http.request":
                # Defensive: forward unexpected message types verbatim by
                # appending; the inner app will see them on replay.
                chunks.append(message)
                continue
            body = message.get("body", b"")
            total += len(body)
            chunks.append(message)
            if total > cap:
                await _send_413(send, cap=cap, observed=total)
                return
            more = bool(message.get("more_body", False))

        replay_index = 0

        async def replay() -> Message:
            nonlocal replay_index
            if replay_index < len(chunks):
                chunk = chunks[replay_index]
                replay_index += 1
                return chunk
            # After we've replayed every buffered chunk, hand control back
            # to the real receive in case the app expects a final
            # disconnect-style message (rare for parsed-body handlers).
            return await receive()

        await self._app(scope, replay, send)
