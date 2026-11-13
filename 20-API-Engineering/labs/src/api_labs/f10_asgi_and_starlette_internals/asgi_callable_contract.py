"""Raw ASGI 3.0 callable contract: scope inspection, receive() draining, send() state machine.

No framework is involved: the application below is a bare ``async def app(scope, receive,
send)``, and the drivers feed it to the pinned uvicorn and Hypercorn internals in-process so
the tests can compare how each server enforces the same send-event order.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class ASGIContractError(RuntimeError):
    """Raised out of send() when an application breaks the HTTP send-event contract."""


class SendState(enum.Enum):
    """States of the ASGI HTTP response as seen from the server side of send()."""

    AWAITING_START = "awaiting-start"
    STREAMING_BODY = "streaming-body"
    AWAITING_TRAILERS = "awaiting-trailers"
    COMPLETE = "complete"


def _validate_headers(headers: Iterable[Any]) -> list[tuple[bytes, bytes]]:
    """Enforce the ASGI HTTP response header rules: byte pairs, lowercase, no pseudo, no CRLF."""
    validated: list[tuple[bytes, bytes]] = []
    for pair in headers:
        name, value = pair
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            raise ASGIContractError("header names and values must be byte strings")
        if name != name.lower():
            raise ASGIContractError(f"response header name {name!r} must be lowercased")
        if name.startswith(b":"):
            raise ASGIContractError(f"pseudo header {name!r} is not allowed")
        if b"\r" in value or b"\n" in value or b"\x00" in value:
            raise ASGIContractError(f"header {name!r} value contains CR, LF, or NUL")
        validated.append((name, value))
    return validated


@dataclass
class ContractCheckingSend:
    """A send() that enforces start -> body* -> (trailers*) -> complete, then forwards."""

    downstream: Send | None = None
    state: SendState = SendState.AWAITING_START
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list[tuple[bytes, bytes]])
    body: bytearray = field(default_factory=bytearray)
    body_events: int = 0
    trailers: list[tuple[bytes, bytes]] = field(default_factory=list[tuple[bytes, bytes]])
    trailers_expected: bool = False

    async def __call__(self, message: Message) -> None:
        kind = message.get("type")
        if self.state is SendState.AWAITING_START:
            if kind != "http.response.start":
                raise ASGIContractError(f"expected 'http.response.start' first, got {kind!r}")
            status = message.get("status")
            if type(status) is not int or not 200 <= status <= 599:
                raise ASGIContractError(f"final status must be an int in 200..599, got {status!r}")
            self.status = status
            self.headers = _validate_headers(message.get("headers", []))
            self.trailers_expected = bool(message.get("trailers", False))
            self.state = SendState.STREAMING_BODY
        elif self.state is SendState.STREAMING_BODY:
            if kind != "http.response.body":
                raise ASGIContractError(f"expected 'http.response.body', got {kind!r}")
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                raise ASGIContractError(f"'body' must be bytes, got {type(body).__name__}")
            self.body += body
            self.body_events += 1
            if not message.get("more_body", False):
                self.state = (
                    SendState.AWAITING_TRAILERS if self.trailers_expected else SendState.COMPLETE
                )
        elif self.state is SendState.AWAITING_TRAILERS:
            if kind != "http.response.trailers":
                raise ASGIContractError(f"expected 'http.response.trailers', got {kind!r}")
            self.trailers.extend(_validate_headers(message.get("headers", [])))
            if not message.get("more_trailers", False):
                self.state = SendState.COMPLETE
        else:
            raise ASGIContractError(f"{kind!r} sent after the response completed")
        if self.downstream is not None:
            await self.downstream(message)


@dataclass
class ScriptedReceive:
    """A receive() that yields one http.request per chunk, then http.disconnect forever."""

    chunks: Sequence[bytes]
    calls: int = 0
    _events: list[Message] = field(init=False, default_factory=list[Message])

    def __post_init__(self) -> None:
        parts = list(self.chunks) or [b""]
        last = len(parts) - 1
        self._events = [
            {"type": "http.request", "body": chunk, "more_body": index < last}
            for index, chunk in enumerate(parts)
        ]

    async def __call__(self) -> Message:
        self.calls += 1
        if self._events:
            return self._events.pop(0)
        return {"type": "http.disconnect"}


async def read_body(receive: Receive) -> tuple[bytes, int] | None:
    """Drain http.request events until more_body is False; None means the client left."""
    chunks: list[bytes] = []
    events = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        if message["type"] != "http.request":
            raise ASGIContractError(f"unexpected receive event {message['type']!r}")
        events += 1
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks), events


async def contract_echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Raw ASGI 3.0 app: report the scope and body it saw, answer in two body events."""
    if scope["type"] != "http":
        raise ValueError(f"unsupported ASGI scope type {scope['type']!r}")
    received = await read_body(receive)
    if received is None:
        return
    body, request_events = received
    asgi: dict[str, str] = scope.get("asgi", {})
    raw_path: bytes | None = scope.get("raw_path")
    headers: Iterable[tuple[bytes, bytes]] = scope["headers"]
    summary: dict[str, object] = {
        "asgi_version": asgi.get("version", "2.0"),
        "spec_version": asgi.get("spec_version", "2.0"),
        "method": scope["method"],
        "path": scope["path"],
        "raw_path": None if raw_path is None else raw_path.decode("latin-1"),
        "query_string": scope["query_string"].decode("latin-1"),
        "root_path": scope.get("root_path", ""),
        "header_names": [name.decode("latin-1") for name, _ in headers],
        "request_events": request_events,
        "body": body.decode("utf-8", "replace"),
    }
    payload = json.dumps(summary, sort_keys=True).encode("ascii")
    half = len(payload) // 2
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload[:half], "more_body": True})
    await send({"type": "http.response.body", "body": payload[half:], "more_body": False})


async def chunked_stream_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Stream two chunks with no Content-Length, so an HTTP/1.1 server must chunk-encode."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"hello", "more_body": True})
    await send({"type": "http.response.body", "body": b"world", "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def send_after_complete_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Bug: finishes the response, then sends one more body event."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"done", "more_body": False})
    await send({"type": "http.response.body", "body": b"late", "more_body": False})


async def body_before_start_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Bug: sends a body event before http.response.start."""
    await send({"type": "http.response.body", "body": b"oops", "more_body": False})


class LegacyASGI2App:
    """ASGI 2.0 double callable: instantiated with scope, then awaited with receive and send."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    async def __call__(self, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        body = b"legacy:" + self.scope["path"].encode()
        await send({"type": "http.response.body", "body": body})


def sync_wrapped(app: ASGIApp) -> ASGIApp:
    """A decorator that forgets ``async``: it returns the inner coroutine from a plain def."""

    def wrapper(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        return app(scope, receive, send)

    return wrapper


def marked_sync_wrapped(app: ASGIApp) -> ASGIApp:
    """The same wrapper, marked so inspect.iscoroutinefunction() reports True (3.12+)."""
    return inspect.markcoroutinefunction(sync_wrapped(app))


def classify_interface(app: object) -> dict[str, str]:
    """Report how asgiref, uvicorn (interface='auto'), and Hypercorn classify an app object."""
    from asgiref.compatibility import is_double_callable
    from hypercorn.utils import is_asgi
    from uvicorn.config import Config

    config = Config(app=app, interface="auto", log_config=None, lifespan="off")
    config.load()
    return {
        "asgiref": "double" if is_double_callable(app) else "single",
        "uvicorn": config.interface,
        "hypercorn": "asgi" if is_asgi(app) else "wsgi",
    }


class RecordingTransport(asyncio.Transport):
    """An asyncio.Transport that records every byte uvicorn writes; no socket is opened."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        self.buffer += data

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        return None

    def resume_reading(self) -> None:
        return None


@dataclass(frozen=True)
class ServerRun:
    """What a server-side driver observed: wire bytes or events, and the send() error text."""

    output: bytes
    events: tuple[str, ...]
    error: str | None


def make_http_scope(method: str) -> dict[str, Any]:
    """Build an HTTP/1.1 scope with the keys uvicorn 0.54.0 sets for ``<method> /``."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "state": {},
    }


async def drive_uvicorn_cycle(
    app: ASGIApp, *, method: str = "GET", request_chunks: Sequence[bytes] = ()
) -> ServerRun:
    """Run app against uvicorn's httptools RequestResponseCycle with a recording transport.

    The request body is fed the way HttpToolsProtocol.on_body and on_message_complete do:
    each parser chunk is appended to cycle.body, then more_body is cleared and the event set.
    """
    from uvicorn.protocols.http.flow_control import FlowControl
    from uvicorn.protocols.http.httptools_impl import RequestResponseCycle

    transport = RecordingTransport()
    cycle = RequestResponseCycle(
        scope=cast("Any", make_http_scope(method)),
        transport=transport,
        flow=FlowControl(transport),
        logger=logging.getLogger("api_labs.uvicorn_cycle"),
        access_logger=logging.getLogger("api_labs.uvicorn_access"),
        access_log=False,
        default_headers=[],
        message_event=asyncio.Event(),
        expect_100_continue=False,
        keep_alive=True,
        on_response=lambda: None,
    )
    for chunk in request_chunks:
        cycle.body += chunk
    cycle.more_body = False
    cycle.message_event.set()
    error: str | None = None
    try:
        await app(
            cast("Scope", cycle.scope), cast("Receive", cycle.receive), cast("Send", cycle.send)
        )
    except RuntimeError as exc:
        error = str(exc)
    return ServerRun(output=bytes(transport.buffer), events=(), error=error)


async def drive_hypercorn_stream(app: ASGIApp) -> ServerRun:
    """Run app against Hypercorn's HTTPStream.app_send and record the protocol events."""
    from hypercorn.config import Config
    from hypercorn.protocol.http_stream import HTTPStream
    from hypercorn.utils import UnexpectedMessageError

    events: list[str] = []

    async def record(event: object) -> None:
        events.append(type(event).__name__)

    stream = HTTPStream(
        app=cast("Any", None),
        config=Config(),
        context=cast("Any", None),
        task_group=cast("Any", None),
        ssl=False,
        client=None,
        server=None,
        send=record,
        stream_id=1,
    )
    stream.scope = cast("Any", make_http_scope("GET"))
    stream.start_time = 0.0

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    error: str | None = None
    try:
        await app(cast("Scope", stream.scope), receive, cast("Send", stream.app_send))
    except UnexpectedMessageError as exc:
        error = str(exc)
    return ServerRun(output=b"", events=tuple(events), error=error)
