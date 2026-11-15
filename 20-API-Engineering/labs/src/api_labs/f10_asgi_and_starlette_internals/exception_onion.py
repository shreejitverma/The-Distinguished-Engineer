"""Starlette exception onion lab: which layer turns an exception into a response, and when none can.

Starlette 1.10.0 builds every application as
ServerErrorMiddleware -> user middleware -> ExceptionMiddleware -> Router.
This module builds such an app with recording middleware so a test can see which layer
observed a response and which layer observed a raised exception, and it serves the same app
on a real Uvicorn socket to capture what a mid-stream exception does to the HTTP/1.1 bytes.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route, Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, ExceptionHandler, Message, Receive, Scope, Send


class TeapotError(Exception):
    """A domain error with a registered class handler (handled by ExceptionMiddleware)."""


class StreamBoom(Exception):
    """An error with no handler, raised by a body iterator after the first chunk."""


class LateFailure(Exception):
    """An error raised by a background task after the response has completed."""


class Recorder:
    """Pure ASGI middleware that logs what it saw: entry, response status, raise, or exit."""

    def __init__(self, app: ASGIApp, name: str, log: list[str]) -> None:
        self.app = app
        self.name = name
        self.log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.log.append(f"{self.name}:enter")

        async def recording_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.log.append(f"{self.name}:start:{message['status']}")
            await send(message)

        try:
            await self.app(scope, receive, recording_send)
        except Exception as exc:
            self.log.append(f"{self.name}:raised:{type(exc).__name__}")
            raise
        self.log.append(f"{self.name}:exit")


async def _chunk_then_raise(exc: Exception) -> AsyncIterator[bytes]:
    yield b"chunk-1\n"
    raise exc


async def ok(request: Request) -> Response:
    return PlainTextResponse("ok")


async def http_exception(request: Request) -> Response:
    raise HTTPException(status_code=418, detail="short and stout")


async def handled(request: Request) -> Response:
    raise TeapotError("brewing")


async def unhandled(request: Request) -> Response:
    raise ValueError("boom")


async def leaky(request: Request) -> Response:
    dsn = "postgresql://app:example-password@db.internal/prod"
    raise ConnectionError(f"cannot reach {dsn}")


async def stream_unhandled(request: Request) -> Response:
    return StreamingResponse(_chunk_then_raise(StreamBoom("generator failed")))


async def stream_handled(request: Request) -> Response:
    return StreamingResponse(_chunk_then_raise(TeapotError("brewing mid-stream")))


async def _fail_later() -> None:
    raise LateFailure("background task failed")


async def background_fails(request: Request) -> Response:
    return PlainTextResponse("accepted", background=BackgroundTask(_fail_later))


async def teapot_handler(request: Request, exc: Exception) -> Response:
    return JSONResponse({"error": "teapot", "detail": str(exc)}, status_code=409)


async def server_error_handler(request: Request, exc: Exception) -> Response:
    return JSONResponse({"error": "internal", "type": type(exc).__name__}, status_code=500)


ROUTES = [
    Route("/ok", ok),
    Route("/http-exception", http_exception),
    Route("/handled", handled),
    Route("/unhandled", unhandled),
    Route("/leaky", leaky),
    Route("/stream-unhandled", stream_unhandled),
    Route("/stream-handled", stream_handled),
    Route("/background-fails", background_fails),
]


def build_app(
    log: list[str],
    *,
    custom_500: bool = False,
    cors: bool = False,
    debug: bool = False,
) -> Starlette:
    """Build the lab app with two recording user middlewares, optionally CORS outside them."""
    middleware = [
        Middleware(Recorder, name="outer", log=log),
        Middleware(Recorder, name="inner", log=log),
    ]
    if cors:
        middleware.insert(0, Middleware(CORSMiddleware, allow_origins=["https://app.example"]))
    handlers: dict[type[Exception], ExceptionHandler] = {TeapotError: teapot_handler}
    if custom_500:
        handlers[Exception] = server_error_handler
    return Starlette(debug=debug, routes=ROUTES, middleware=middleware, exception_handlers=handlers)


def describe_stack(app: Starlette) -> list[str]:
    """Walk the built middleware stack from the outermost layer to the Router."""
    layer: object = app.build_middleware_stack()
    names: list[str] = []
    while True:
        label = type(layer).__name__
        if isinstance(layer, Recorder):
            label = f"Recorder({layer.name})"
        names.append(label)
        if isinstance(layer, Router):
            return names
        layer = vars(layer)["app"]


@asynccontextmanager
async def serve_uvicorn(app: ASGIApp) -> AsyncGenerator[tuple[str, int]]:
    """Run Uvicorn (h11 protocol) on a pre-bound 127.0.0.1:0 socket for the block's duration."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    config = uvicorn.Config(
        app, http="h11", loop="asyncio", lifespan="off", log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("uvicorn did not start within 5 seconds")
        yield "127.0.0.1", port
    finally:
        server.should_exit = True
        await task
        sock.close()


async def raw_http11_exchange(host: str, port: int, target: str) -> bytes:
    """Send one `GET` with `Connection: close` and return every byte until the server closes."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        request = f"GET {target} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n"
        writer.write(request.encode("ascii"))
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()
        await writer.wait_closed()
