"""Pure ASGI middleware that wraps scope, receive, and send without buffering bodies.

Each layer is a plain ASGI 3 callable class that keeps all per-request state in
local variables of ``__call__``:

- ``RequestIdMiddleware`` validates or mints an ``x-request-id``, hands it to the
  inner app through a copied scope, and appends it to ``http.response.start``.
- ``ServerTimingMiddleware`` appends ``server-timing: app;dur=<ms>`` measured from
  call entry to ``http.response.start``.
- ``BodyLimitMiddleware`` rejects a declared oversized ``content-length`` before
  the app runs and counts streamed ``http.request`` bytes, answering ``413`` if
  the response has not started.
- ``StatusRewriteMiddleware`` rewrites ``status`` on ``http.response.start``.

Body bytes are forwarded by reference: no layer joins, slices, or copies a chunk.
Header edits build a new message dict and a new header list, so a response object
that the inner app reuses across requests is never mutated.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_SCOPE_KEY: Final = "api_labs.request_id"
_REQUEST_ID_RE: Final = re.compile(rb"[A-Za-z0-9._-]{1,64}")
_CONTENT_LENGTH_RE: Final = re.compile(rb"[0-9]{1,19}")
_HTTP1_VERSIONS: Final = frozenset({"1.0", "1.1"})

type RawHeaders = Iterable[tuple[bytes, bytes]]


def header_values(headers: RawHeaders, name: bytes) -> list[bytes]:
    """Return every value of ``name`` (lowercase) in ASGI raw headers, in order."""
    return [value for key, value in headers if key == name]


def with_appended_header(message: Message, name: bytes, value: bytes) -> Message:
    """Return a shallow copy of ``message`` with one header appended.

    The body (if any) and every existing header tuple are shared, not copied.
    """
    headers = [*message.get("headers", ()), (name, value)]
    return {**message, "headers": headers}


def generate_request_id() -> str:
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Propagate a validated ``x-request-id`` into the scope and the response."""

    def __init__(
        self, app: ASGIApp, *, id_factory: Callable[[], str] = generate_request_id
    ) -> None:
        self.app = app
        self.id_factory = id_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = header_values(scope["headers"], b"x-request-id")
        if len(inbound) == 1 and _REQUEST_ID_RE.fullmatch(inbound[0]):
            request_id = inbound[0]
        else:
            request_id = self.id_factory().encode("ascii")

        # ASGI spec, Middleware: copy the scope before adding keys.
        child_scope = {**scope, REQUEST_ID_SCOPE_KEY: request_id.decode("ascii")}

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = with_appended_header(message, b"x-request-id", request_id)
            await send(message)

        await self.app(child_scope, receive, send_with_request_id)


class ServerTimingMiddleware:
    """Append a W3C ``server-timing`` metric for time-to-response-start."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metric: str = "app",
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.app = app
        self.metric = metric.encode("ascii")
        self.clock_ns = clock_ns

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_ns = self.clock_ns()

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = (self.clock_ns() - started_ns) / 1_000_000
                value = self.metric + b";dur=" + f"{elapsed_ms:.3f}".encode("ascii")
                message = with_appended_header(message, b"server-timing", value)
            await send(message)

        await self.app(scope, receive, send_with_timing)


class BodyTooLargeError(Exception):
    """Raised out of the wrapped ``receive`` once the body exceeds the limit."""


class _InvalidContentLength:
    __slots__ = ()


INVALID_CONTENT_LENGTH: Final = _InvalidContentLength()


def declared_content_length(scope: Scope) -> int | _InvalidContentLength | None:
    """Parse ``content-length`` strictly per RFC 9110 Section 8.6 (``1*DIGIT``).

    Returns ``None`` when absent or when ``transfer-encoding`` is present, because
    Transfer-Encoding overrides Content-Length (RFC 9112 Section 6.3, rule 3).
    A repeated identical value (``42, 42``) is accepted (RFC 9112 Section 6.3, rule 5).
    """
    headers: list[tuple[bytes, bytes]] = list(scope["headers"])
    if header_values(headers, b"transfer-encoding"):
        return None
    raw_values = header_values(headers, b"content-length")
    if not raw_values:
        return None
    parts = [part.strip(b" \t") for value in raw_values for part in value.split(b",")]
    if not all(_CONTENT_LENGTH_RE.fullmatch(part) for part in parts):
        return INVALID_CONTENT_LENGTH
    lengths = {int(part) for part in parts}
    if len(lengths) != 1:
        return INVALID_CONTENT_LENGTH
    return lengths.pop()


async def send_plain_error(scope: Scope, send: Send, status: int, body: bytes) -> None:
    """Send a complete plain-text error, asking HTTP/1.x servers to close."""
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    # RFC 9113 Section 8.2.2: Connection is forbidden in HTTP/2 messages.
    if scope.get("http_version", "1.1") in _HTTP1_VERSIONS:
        headers.append((b"connection", b"close"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class BodyLimitMiddleware:
    """Enforce a request body limit in flight, without buffering the body."""

    def __init__(self, app: ASGIApp, *, max_body_size: int) -> None:
        if max_body_size < 0:
            raise ValueError("max_body_size must be non-negative")
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = declared_content_length(scope)
        if isinstance(declared, _InvalidContentLength):
            await send_plain_error(scope, send, 400, b"Invalid Content-Length")
            return
        if declared is not None and declared > self.max_body_size:
            # Reject from the headers alone: receive() is never called, so a server
            # that sends 100 Continue lazily (uvicorn) never invites the body.
            await send_plain_error(scope, send, 413, b"Content Too Large")
            return

        received = 0
        exceeded = False
        response_started = False

        async def receive_with_limit() -> Message:
            nonlocal received, exceeded
            if exceeded:
                raise BodyTooLargeError
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_size:
                    exceeded = True
                    raise BodyTooLargeError
            return message

        async def send_tracking_start(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                # The app swallowed BodyTooLargeError; never answer a truncated body.
                raise BodyTooLargeError
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_with_limit, send_tracking_start)
        except BodyTooLargeError:
            if response_started:
                raise
            await send_plain_error(scope, send, 413, b"Content Too Large")
            return
        if exceeded and not response_started:
            await send_plain_error(scope, send, 413, b"Content Too Large")


class StatusRewriteMiddleware:
    """Rewrite response status codes on ``http.response.start``.

    Targets that forbid a body (1xx, ``204``, ``304``) are refused, because the
    inner app's body and ``content-length`` would then violate RFC 9110 Section 8.6.
    """

    def __init__(self, app: ASGIApp, *, mapping: Mapping[int, int]) -> None:
        for target in mapping.values():
            if target < 200 or target in (204, 304) or target > 599:
                raise ValueError(f"cannot rewrite to status {target}")
        self.app = app
        self.mapping = dict(mapping)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                if status in self.mapping:
                    message = {**message, "status": self.mapping[status]}
            await send(message)

        await self.app(scope, receive, send_with_status)


class InPlaceHeaderAppendMiddleware:
    """Anti-pattern kept for the lab: appends to the inner app's header list in place."""

    def __init__(self, app: ASGIApp, *, name: bytes, value: bytes) -> None:
        self.app = app
        self.name = name
        self.value = value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_appending(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"].append((self.name, self.value))
            await send(message)

        await self.app(scope, receive, send_appending)
