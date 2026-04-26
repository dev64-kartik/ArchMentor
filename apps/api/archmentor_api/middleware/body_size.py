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

For ALL capped requests, the middleware streams and counts actual body
bytes regardless of whether a `Content-Length` header is present. The
only fast-path is: if `Content-Length` is declared and *exceeds* the cap,
reject immediately without reading the body. In all other cases (no C-L,
or C-L within cap), the middleware reads and counts bytes so a client
that declares `Content-Length: 100` but sends 10 MB cannot bypass the
gate (the "Content-Length lie" attack).
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
    """Return the Content-Length header as an int, or None if missing/invalid/negative.

    `name.lower()` is applied for safety — uvicorn normalizes header names to
    lowercase, but other ASGI servers (e.g. Hypercorn) may not. A negative
    Content-Length is invalid per RFC 9110 and treated as missing so the
    streaming-cap path runs instead.
    """
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                parsed = int(value)
            except ValueError:
                return None
            if parsed < 0:
                return None
            return parsed
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
        if declared is not None and declared > cap:
            # Fast-path: Content-Length already proves the body is too large.
            # No need to read a single byte.
            await _send_413(send, cap=cap, observed=declared)
            return

        # Always stream-and-count the body. When Content-Length is declared
        # and within cap, we still read bytes to catch the "Content-Length lie"
        # — a client claiming C-L: 100 but sending 10 MB would otherwise bypass
        # the gate entirely. The effective per-chunk cap is `min(cap, declared)`
        # so we abort the moment observed bytes exceed either bound.
        await self._gate_streamed_body(scope, receive, send, cap=cap, declared=declared)

    async def _gate_streamed_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        cap: int,
        declared: int | None = None,
    ) -> None:
        # If C-L was declared and within cap, cap the running counter at
        # `min(cap, declared)` as defense-in-depth: any byte beyond the
        # declared length is evidence of a lie, and we abort with 413.
        effective_cap = min(cap, declared) if declared is not None else cap
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
            if total > effective_cap:
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
