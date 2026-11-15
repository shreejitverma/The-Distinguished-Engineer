"""Starlette request body consumption: per-instance _body caching, starvation, and ASGI replay."""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.middleware import Middleware
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass
class BodyLog:
    """What a logging middleware observed about each request body."""

    sizes: list[int] = field(default_factory=list[int])
    chunk_counts: list[int] = field(default_factory=list[int])


class ScriptedReceive:
    """A server-side receive() that returns queued messages without suspending.

    Uvicorn returns immediately when its message_event is already set and Hypercorn's
    receive is asyncio.Queue.get, so a buffered body message is handed over without a
    checkpoint. Once the script is exhausted it returns http.disconnect, which models a
    client that gave up waiting for a response.
    """

    def __init__(self, messages: list[Message]) -> None:
        self._messages: deque[Message] = deque(messages)
        self.calls = 0

    async def __call__(self) -> Message:
        self.calls += 1
        if self._messages:
            return self._messages.popleft()
        return {"type": "http.disconnect"}


def body_messages(*chunks: bytes) -> list[Message]:
    """Frame chunks as http.request events; only the last one has more_body False."""
    last = len(chunks) - 1
    return [
        {"type": "http.request", "body": chunk, "more_body": index != last}
        for index, chunk in enumerate(chunks)
    ]


class NaiveBodyLoggerMiddleware:
    """The bug: drains the stream through its own Request, then forwards the drained receive."""

    def __init__(self, app: ASGIApp, log: BodyLog) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        size = 0
        chunks = 0
        async for chunk in request.stream():
            size += len(chunk)
            chunks += 1 if chunk else 0
        self.log.sizes.append(size)
        self.log.chunk_counts.append(chunks)
        await self.app(scope, receive, send)  # downstream Request starts from a drained channel


class BodyReplayMiddleware:
    """The fix: buffer the http.request events once, enforce a cap, replay them downstream."""

    def __init__(self, app: ASGIApp, log: BodyLog, max_body_size: int) -> None:
        self.app = app
        self.log = log
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_size:
            await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)
            return
        buffered: deque[Message] = deque()
        size = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break  # http.disconnect: replay it so the handler sees ClientDisconnect
            size += len(message.get("body", b""))
            if size > self.max_body_size:
                await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        self.log.sizes.append(size)
        self.log.chunk_counts.append(sum(1 for m in buffered if m.get("body")))

        async def replay() -> Message:
            if buffered:
                return buffered.popleft()
            return await receive()  # after the body: wait for the real http.disconnect

        await self.app(scope, replay, send)


class StreamPeekingMiddleware(BaseHTTPMiddleware):
    """Iterates request.stream() in dispatch; call_next then hands the endpoint b''."""

    def __init__(self, app: ASGIApp, log: BodyLog, reread_after: bool = False) -> None:
        super().__init__(app)
        self.log = log
        self.reread_after = reread_after

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
        self.log.sizes.append(size)
        response = await call_next(request)
        if self.reread_after:
            await request.body()  # same instance, stream drained, no _body: Stream consumed
        return response


class BodyReadingMiddleware(BaseHTTPMiddleware):
    """Awaits request.body() in dispatch; _CachedRequest replays _body to the endpoint."""

    def __init__(self, app: ASGIApp, log: BodyLog) -> None:
        super().__init__(app)
        self.log = log

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        self.log.sizes.append(len(await request.body()))
        return await call_next(request)


async def echo(request: Request) -> JSONResponse:
    """Report what the endpoint's own Request saw of the body."""
    body = await request.body()
    return JSONResponse({"length": len(body), "sha256": hashlib.sha256(body).hexdigest()})


async def count_chunks(request: Request) -> JSONResponse:
    """Stream the body and report the chunk boundaries the endpoint observed."""
    sizes = [len(chunk) async for chunk in request.stream() if chunk]
    return JSONResponse({"chunks": sizes})


async def stream_then_body(request: Request) -> JSONResponse:
    """Drain the stream, then ask for the body on the same instance."""
    async for _ in request.stream():
        pass
    return JSONResponse({"length": len(await request.body())})


async def body_then_stream(request: Request) -> JSONResponse:
    """Read the body, then stream: stream() replays the cached _body as one chunk."""
    body = await request.body()
    replayed = [chunk async for chunk in request.stream()]
    return JSONResponse({"length": len(body), "replayed": [len(c) for c in replayed]})


async def peek_then_body(request: Request) -> JSONResponse:
    """Call is_disconnected() before reading: a ready http.request message is discarded."""
    disconnected = await request.is_disconnected()
    body = await request.body()
    return JSONResponse({"disconnected": disconnected, "length": len(body)})


def build_app(
    middleware: list[Middleware] | None = None, max_body_size: int | None = None
) -> Starlette:
    """Build the lab app; the endpoints only differ in how they consume the body."""
    return Starlette(
        routes=[
            Route("/echo", echo, methods=["POST"]),
            Route("/chunks", count_chunks, methods=["POST"]),
            Route("/stream-then-body", stream_then_body, methods=["POST"]),
            Route("/body-then-stream", body_then_stream, methods=["POST"]),
            Route("/peek-then-body", peek_then_body, methods=["POST"]),
        ],
        middleware=middleware,
        max_body_size=max_body_size,
    )
