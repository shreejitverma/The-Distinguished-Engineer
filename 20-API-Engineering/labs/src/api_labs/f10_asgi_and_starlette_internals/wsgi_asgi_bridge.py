"""A PEP 3333 gateway inside an ASGI 3.0 app, next to a native ASGI app.

`WSGIBridge` runs a synchronous WSGI callable from an ASGI server the only way
WSGI allows: one worker thread per in-flight request, borrowed from a bounded
`anyio.CapacityLimiter`. It follows the PEP 3333 rules that the popular bridges
skip (delayed headers, `exc_info` replacement, `write()`, `close()`, string
`SERVER_PORT`, `; `-joined cookie crumbs), and it shows what no bridge can
fix: a held response holds a thread, a disconnect is seen only at the next
write, and `websocket` and `lifespan` scopes have nothing to map to.

`NativeASGIApp` is the counterpart: every connection is a coroutine on one
event loop, `receive()` reports `http.disconnect` while the app is idle, and the
same callable answers `http`, `websocket`, and `lifespan` scopes.
"""

from __future__ import annotations

import io
import sys
import threading
from typing import TYPE_CHECKING

import anyio
import anyio.from_thread
import anyio.to_thread

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType
    from wsgiref.types import WSGIApplication, WSGIEnvironment

    from starlette.types import Message, Receive, Scope, Send

    type OptExcInfo = (
        tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]
    )

# RFC 2616 Section 13.5.1 hop-by-hop headers (the list PEP 3333 points to) plus
# RFC 9110's `Trailer` spelling; PEP 3333 forbids applications to send them.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


class UnsupportedScopeError(Exception):
    """Raised for an ASGI scope type that a WSGI callable cannot express."""


def build_environ(scope: Scope, body: bytes) -> WSGIEnvironment:
    """Map an ASGI `http` scope to a PEP 3333 environ (ASGI HTTP spec, WSGI Compatibility)."""
    script_name: str = scope.get("root_path", "")
    path: str = scope["path"]
    if path == script_name or path.startswith(script_name.rstrip("/") + "/"):
        path = path[len(script_name) :]
    server = scope.get("server") or ("localhost", 80)
    environ: WSGIEnvironment = {
        "REQUEST_METHOD": scope["method"],
        # PEP 3333 "native strings": text travels as latin-1 code points.
        "SCRIPT_NAME": script_name.encode("utf-8").decode("latin-1"),
        "PATH_INFO": path.encode("utf-8").decode("latin-1"),
        "QUERY_STRING": scope["query_string"].decode("latin-1"),
        "SERVER_NAME": str(server[0]),
        "SERVER_PORT": str(server[1] if server[1] is not None else 80),
        "SERVER_PROTOCOL": f"HTTP/{scope['http_version']}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if scope.get("client"):
        environ["REMOTE_ADDR"] = str(scope["client"][0])
    for raw_name, raw_value in scope["headers"]:
        name = raw_name.decode("latin-1")
        if name == "content-type":
            key = "CONTENT_TYPE"
        elif name == "content-length":
            key = "CONTENT_LENGTH"
        else:
            key = "HTTP_" + name.upper().replace("-", "_")
        value = raw_value.decode("latin-1")
        if key in environ:
            # RFC 9113 Section 8.2.3: HTTP/2 cookie crumbs rejoin with "; ", not ",".
            separator = "; " if key == "HTTP_COOKIE" else ","
            value = environ[key] + separator + value
        environ[key] = value
    return environ


def _check_header(name: str, value: str) -> None:
    for text in (name, value):
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
            raise ValueError(f"control character in response header {name!r}")
    if name.lower() in HOP_BY_HOP:
        raise ValueError(f"hop-by-hop header {name!r} is reserved to the server")


class _Response:
    """Per-request response state shared by `start_response`, `write`, and the iterator."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.headers_sent = False

    async def _forward(self, message: Message) -> None:
        await self._send(message)

    def _send_from_thread(self, message: Message) -> None:
        # Blocks this worker thread until the server accepted the message: the
        # PEP 3333 "Buffering and Streaming" guarantee doubles as backpressure.
        anyio.from_thread.run(self._forward, message)

    def start_response(
        self,
        status: str,
        headers: list[tuple[str, str]],
        exc_info: OptExcInfo | None = None,
        /,
    ) -> Callable[[bytes], object]:
        if exc_info is not None and exc_info[1] is not None:
            try:
                if self.headers_sent:
                    raise exc_info[1].with_traceback(exc_info[2])
            finally:
                exc_info = None  # PEP 3333: hold no reference to the traceback.
        elif self.status is not None:
            raise RuntimeError("start_response called twice without exc_info")
        code, _, reason = status.partition(" ")
        if len(code) != 3 or not code.isdigit() or not reason or not reason.isprintable():
            raise ValueError(f"malformed WSGI status {status!r}")
        for name, value in headers:
            _check_header(name, value)
        self.status = int(code)
        self.headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers
        ]
        return self.write

    def write(self, data: bytes) -> None:
        if not data:
            return  # PEP 3333: headers wait for the first non-empty bytestring.
        if self.status is None:
            raise RuntimeError("body data before start_response")
        if not self.headers_sent:
            self._send_from_thread(
                {"type": "http.response.start", "status": self.status, "headers": self.headers}
            )
            self.headers_sent = True
        self._send_from_thread({"type": "http.response.body", "body": data, "more_body": True})

    def finish(self) -> None:
        if self.status is None:
            raise RuntimeError("WSGI application returned without calling start_response")
        if not self.headers_sent:
            self._send_from_thread(
                {"type": "http.response.start", "status": self.status, "headers": self.headers}
            )
            self.headers_sent = True
        self._send_from_thread({"type": "http.response.body", "body": b"", "more_body": False})


class WSGIBridge:
    """ASGI 3.0 application that serves a WSGI callable from a bounded thread pool."""

    def __init__(
        self, app: WSGIApplication, *, limiter: anyio.CapacityLimiter, max_body: int = 1 << 20
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Close before accept: the server answers the handshake with 403.
            await send({"type": "websocket.close", "code": 1000})
            return
        if scope["type"] != "http":
            # ASGI 3.0 "Protocol Specifications": reject unknown protocols with an
            # exception, so a lifespan server proceeds without lifespan events.
            raise UnsupportedScopeError(scope["type"])
        body = bytearray()
        while True:  # Buffer on the event loop: a slow upload costs no thread.
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > self.max_body:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            if not message.get("more_body", False):
                break
        environ = build_environ(scope, bytes(body))
        await anyio.to_thread.run_sync(self._run, environ, send, limiter=self.limiter)

    def _run(self, environ: WSGIEnvironment, send: Send) -> None:
        response = _Response(send)
        result = self.app(environ, response.start_response)
        try:
            for chunk in result:
                response.write(chunk)
            response.finish()
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()  # PEP 3333: MUST be called, on success, error, or disconnect.


class NativeASGIApp:
    """One callable for `http`, `websocket`, and `lifespan`, all on the event loop."""

    def __init__(self) -> None:
        self.release = anyio.Event()
        self.started = False
        self.stopped = False
        self.open_streams = 0
        self.disconnects_seen = 0
        self.thread_ids: set[int] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.thread_ids.add(threading.get_ident())
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "websocket":
            await self._echo(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)
        else:
            raise UnsupportedScopeError(scope["type"])

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self.started = True
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                self.stopped = True
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _echo(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.accept"})
            elif message["type"] == "websocket.receive":
                await send({"type": "websocket.send", "text": message.get("text")})
            else:  # websocket.disconnect
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        more_body = True
        while more_body:
            message = await receive()
            more_body = message.get("more_body", False)
        if scope["path"] != "/stream":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        self.open_streams += 1
        async with anyio.create_task_group() as tg:

            async def watch_disconnect() -> None:
                if (await receive())["type"] == "http.disconnect":
                    self.disconnects_seen += 1
                tg.cancel_scope.cancel()

            tg.start_soon(watch_disconnect)
            await self.release.wait()
            await send({"type": "http.response.body", "body": b"last"})
            tg.cancel_scope.cancel()
        self.open_streams -= 1
