"""Starlette 1.10.0 response internals: a scripted ASGI peer and deterministic stream cleanup.

``ScriptedPeer`` plays the server side of one HTTP request in process: it records every
message the application sends, vanishes after a chosen number of body messages, and then
behaves like one of three real servers (drop the send, raise ``OSError``, or hang in it).
``ClosingStreamingResponse`` closes its async generator on every exit path, which plain
``StreamingResponse`` does not do when the generator is suspended at a ``yield``.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import Scope, Send

AfterDisconnect = Literal["drop", "raise", "hang"]
STARLETTE_FILE_CHUNK = 64 * 1024


def http_scope(
    *,
    spec_version: str | None = "2.3",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    extensions: dict[str, dict[str, object]] | None = None,
) -> Scope:
    """Build a minimal HTTP connection scope; ``spec_version=None`` omits the key (means 2.0)."""
    asgi: dict[str, str] = {"version": "3.0"}
    if spec_version is not None:
        asgi["spec_version"] = spec_version
    scope: Scope = {
        "type": "http",
        "asgi": asgi,
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
    }
    if extensions is not None:
        scope["extensions"] = extensions
    return scope


@dataclass
class ScriptedPeer:
    """Server side of one request: records sends, disconnects after N body messages.

    ``after_disconnect`` picks the server behavior once the client is gone:
    ``"drop"`` returns silently (Uvicorn 0.54.0 h11 and httptools ``send``),
    ``"raise"`` raises ``OSError`` (ASGI HTTP spec 2.4), and ``"hang"`` never
    returns from the send that observed the disconnect (a write stuck on a dead socket).
    """

    disconnect_after_bodies: int | None = None
    after_disconnect: AfterDisconnect = "drop"
    sent: list[Message] = field(default_factory=list[Message])
    dropped: int = 0
    receive_calls: int = 0
    _request_delivered: bool = False
    _gone: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def gone(self) -> bool:
        return self._gone.is_set()

    async def receive(self) -> Message:
        self.receive_calls += 1
        if not self._request_delivered:
            self._request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if self._gone.is_set():
            if self.after_disconnect == "raise":
                raise OSError("client disconnected")
            self.dropped += 1
            return
        self.sent.append(message)
        bodies = sum(1 for m in self.sent if m["type"] == "http.response.body")
        if self.disconnect_after_bodies is not None and bodies >= self.disconnect_after_bodies:
            self._gone.set()
            if self.after_disconnect == "hang":
                await asyncio.Event().wait()

    def body_messages(self) -> list[tuple[int, bool]]:
        """``(len(body), more_body)`` for every recorded ``http.response.body`` message."""
        return [
            (len(m.get("body", b"")), bool(m.get("more_body", False)))
            for m in self.sent
            if m["type"] == "http.response.body"
        ]


class ClosingStreamingResponse(StreamingResponse):
    """``StreamingResponse`` whose async generator is closed before ``background`` runs.

    ``stream_response`` is the coroutine Starlette cancels on ``http.disconnect`` (spec < 2.4)
    and the one that sees ``OSError`` from ``send`` (spec >= 2.4), so closing in its
    ``finally`` covers both paths; the shield lets ``aclose()`` finish inside a cancelled scope.
    """

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            aclose: Callable[[], Awaitable[object]] | None = getattr(
                self.body_iterator, "aclose", None
            )
            if aclose is not None:
                with anyio.CancelScope(shield=True):
                    await aclose()


def file_chunk_plan(size: int, chunk_size: int = STARLETTE_FILE_CHUNK) -> list[tuple[int, bool]]:
    """Predict ``FileResponse._handle_simple`` body messages: a full chunk means more follows."""
    plan: list[tuple[int, bool]] = []
    remaining = size
    while True:
        chunk = min(chunk_size, remaining)
        remaining -= chunk
        more = chunk == chunk_size
        plan.append((chunk, more))
        if not more:
            return plan


def starlette_etag(st_mtime: float, st_size: int) -> str:
    """Recompute the ``ETag`` Starlette 1.10.0 derives from ``os.stat`` (not from content)."""
    base = f"{st_mtime}-{st_size}".encode()
    return '"' + hashlib.md5(base, usedforsecurity=False).hexdigest() + '"'
