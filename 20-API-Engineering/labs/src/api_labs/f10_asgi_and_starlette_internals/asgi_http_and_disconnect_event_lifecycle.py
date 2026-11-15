"""ASGI HTTP message cycle: streamed bodies, disconnect, and zero-copy send choice.

The driver is an in-process stand-in for one HTTP connection scope.
It does not open a socket.
Spec version 2.4 says ``send()`` on a closed connection raises a server-specific
``OSError``. Uvicorn 0.54.0 HTTP does not do that; ``raise_on_send_after_close``
selects the two behaviors.
"""

from __future__ import annotations

import io
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class ConnectionClosed(OSError):
    """OSError subclass a spec-2.4 server raises from ``send()`` after close."""


def http_scope(
    *,
    spec_version: str = "2.4",
    extensions: dict[str, dict[str, Any]] | None = None,
    method: str = "POST",
) -> dict[str, Any]:
    """Build a minimal ``type=http`` scope. ``extensions`` is omitted when ``None``."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 9),
        "server": ("127.0.0.1", 80),
    }
    if extensions is not None:
        scope["extensions"] = extensions
    return scope


@dataclass
class ScriptedHTTPCycle:
    """One connection scope with a finite inbound queue and a record of ``send`` calls."""

    inbound: list[dict[str, Any]]
    raise_on_send_after_close: bool = True
    sent: list[dict[str, Any]] = field(default_factory=list)
    response_complete: bool = False
    disconnected: bool = False

    async def receive(self) -> dict[str, Any]:
        """Return the next scripted event, or ``http.disconnect`` once the response ended."""
        if self.inbound:
            message = self.inbound.pop(0)
            if message.get("type") == "http.disconnect":
                self.disconnected = True
            return message
        if self.disconnected or self.response_complete:
            self.disconnected = True
            return {"type": "http.disconnect"}
        raise RuntimeError("receive() called with no scripted event and an open response")

    async def send(self, message: dict[str, Any]) -> None:
        """Record ``message`` or apply the closed-connection policy."""
        if self.disconnected or self.response_complete:
            if self.raise_on_send_after_close:
                raise ConnectionClosed("send() on a closed ASGI HTTP connection")
            return
        kind = message.get("type")
        terminal_body = kind == "http.response.body" and not message.get("more_body", False)
        terminal_zero = kind == "http.response.zerocopysend" and not message.get("more_body", False)
        if terminal_body or terminal_zero or kind == "http.response.pathsend":
            self.response_complete = True
        self.sent.append(message)


@dataclass
class Downstream:
    """Chunks the app pulls only as it sends them. ``pulled`` counts work actually started."""

    chunks: tuple[bytes, ...]
    pulled: int = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk


@dataclass(frozen=True, slots=True)
class StreamOutcome:
    """What the app did with one request: body bytes seen, chunks pulled, why it stopped."""

    request_body: bytes
    chunks_pulled: int
    stopped_on: str


async def read_request_body(receive: Receive) -> tuple[bytes, bool]:
    """Concatenate ``http.request`` bodies.

    Returns ``(body, True)`` when ``http.disconnect`` arrives before a chunk with
    ``more_body`` false. A missing ``more_body`` key defaults to false.
    """
    parts: list[bytes] = []
    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "http.disconnect":
            return b"".join(parts), True
        if kind != "http.request":
            raise RuntimeError(f"expected http.request or http.disconnect, got {kind!r}")
        body = message.get("body", b"")
        if not isinstance(body, bytes):
            raise RuntimeError("http.request body must be bytes")
        parts.append(body)
        if not message.get("more_body", False):
            return b"".join(parts), False


async def stream_with_disconnect(
    receive: Receive,
    send: Send,
    downstream: Downstream,
    *,
    poll_between_chunks: bool,
) -> StreamOutcome:
    """Stream ``downstream`` until the request ends, ``http.disconnect``, or ``OSError``.

    A disconnect observed while the request body is still open sends no response.
    With ``poll_between_chunks``, each successful body ``send`` is followed by
    ``receive()``, and ``http.disconnect`` leaves the remaining chunks unpulled.
    """
    body, disconnected = await read_request_body(receive)
    if disconnected:
        return StreamOutcome(body, downstream.pulled, "http.disconnect")

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/octet-stream")],
        }
    )
    stopped = "complete"
    for chunk in downstream:
        try:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except OSError:
            stopped = "send_oserror"
            break
        if poll_between_chunks:
            probe = await receive()
            if probe.get("type") == "http.disconnect":
                stopped = "http.disconnect"
                break
    else:
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except OSError:
            stopped = "send_oserror"
    return StreamOutcome(body, downstream.pulled, stopped)


def file_is_sendfile_source(file: object) -> bool:
    """True when ``file`` exposes an OS fd that ``os.sendfile`` can use. ``BytesIO`` does not."""
    fileno = getattr(file, "fileno", None)
    if not callable(fileno):
        return False
    try:
        descriptor = fileno()
    except (OSError, AttributeError, io.UnsupportedOperation):
        return False
    return isinstance(descriptor, int) and descriptor >= 0


def zero_copy_or_body(
    extensions: Mapping[str, Mapping[str, Any]],
    path: str,
    file: io.BufferedIOBase,
) -> dict[str, Any]:
    """Choose one terminal body message. Never mix ``pathsend`` with ``http.response.body``.

    ``http.response.pathsend`` wins when advertised, and its path must be absolute.
    ``http.response.zerocopysend`` is next, only for a real file descriptor.
    Otherwise the bytes are copied into ``http.response.body``. The caller closes ``file``.
    """
    if "http.response.pathsend" in extensions:
        if not os.path.isabs(path):
            raise ValueError("http.response.pathsend path must be absolute")
        return {"type": "http.response.pathsend", "path": path}
    if "http.response.zerocopysend" in extensions and file_is_sendfile_source(file):
        return {"type": "http.response.zerocopysend", "file": file, "more_body": False}
    data = file.read()
    if not isinstance(data, bytes):
        raise RuntimeError("file read did not return bytes")
    return {"type": "http.response.body", "body": data, "more_body": False}


async def echo_request_body(scope: dict[str, Any], receive: Receive, send: Send) -> None:
    """ASGI app that joins the request body and sends it back as one response body."""
    if scope.get("type") != "http":
        raise RuntimeError(f"echo_request_body requires type 'http', got {scope.get('type')!r}")
    body, disconnected = await read_request_body(receive)
    if disconnected:
        return
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/octet-stream")],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
