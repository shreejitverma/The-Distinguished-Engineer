"""Cancel upstream GPU inference when the ASGI client disconnects mid-stream.

A deterministic, socket-free model of the chain
``client -> ASGI server -> gateway -> httpx -> inference engine``.
``FakeGPUEngine`` stands in for a continuous-batching engine: it holds one slot per
admitted request, runs one decode step per released token, and records whether the
request was aborted or ran to completion.
``EngineByteStream`` exposes it to ``httpx`` as an SSE byte stream through
``httpx.MockTransport``, so the real ``httpx.Response.aclose`` path runs.
``ASGIConnection`` is a scripted ASGI connection with a controllable client side.

Three gateways differ only in how they notice the disconnect:

* ``buffering_gateway`` decouples a producer from the response and never listens.
* ``starlette_gateway`` relies on ``starlette.responses.StreamingResponse``.
* ``watching_gateway`` races the token pump against ``receive()`` and shields cleanup.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    MutableMapping,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from anyio.lowlevel import checkpoint
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

Message = MutableMapping[str, Any]
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Coroutine[Any, Any, None]]

UPSTREAM_URL = "http://gpu.internal/v1/completions"


class ConnectionClosed(OSError):
    """The ``OSError`` subclass an ASGI spec 2.4 server raises from ``send()`` after close."""


class FakeGPUEngine:
    """One request's worth of engine state: slot accounting, decode steps, abort record.

    ``release(n)`` allows ``n`` decode steps; with ``free_run`` the engine decodes as
    fast as the loop schedules it.
    ``prefill_gate`` models queueing plus prefill: no token exists until it is set.
    """

    def __init__(self, *, max_tokens: int = 50, free_run: bool = False) -> None:
        self.max_tokens = max_tokens
        self.free_run = free_run
        self.prefill_gate = asyncio.Event()
        self.prefill_gate.set()
        self.admitted = asyncio.Event()
        self.done = asyncio.Event()
        self.upstream_closed_event = asyncio.Event()
        self.permits = asyncio.Semaphore(0)
        self.admissions = 0
        self.slots_in_use = 0
        self.steps = 0
        self.finished = False
        self.aborted = False
        self.aborted_at: float | None = None
        self.upstream_closed = False
        self.upstream_closed_at: float | None = None

    def release(self, count: int) -> None:
        """Allow ``count`` more decode steps."""
        for _ in range(count):
            self.permits.release()

    async def generate(self) -> AsyncGenerator[str, None]:
        """Admit one request, then yield one token per decode step."""
        self.admissions += 1
        self.slots_in_use += 1
        self.admitted.set()
        try:
            await self.prefill_gate.wait()
            for index in range(self.max_tokens):
                if self.free_run:
                    await asyncio.sleep(0)
                else:
                    await self.permits.acquire()
                self.steps += 1
                yield f"tok{index}"
            self.finished = True
        except (asyncio.CancelledError, GeneratorExit):
            self.aborted = True
            self.aborted_at = time.perf_counter()
            raise
        finally:
            self.slots_in_use -= 1
            self.done.set()


class EngineByteStream(httpx.AsyncByteStream):
    """SSE bytes for one engine request; ``aclose`` is what ``httpx`` calls on release.

    ``close_checkpoints`` makes ``aclose`` await once before it records the close, the
    way a real network close does, so cancellation can interrupt it.
    """

    def __init__(self, engine: FakeGPUEngine, *, close_checkpoints: bool) -> None:
        self._engine = engine
        self._tokens = engine.generate()
        self._close_checkpoints = close_checkpoints

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for token in self._tokens:
            yield f"data: {token}\n\n".encode("ascii")

    async def aclose(self) -> None:
        if self._close_checkpoints:
            await checkpoint()
        await self._tokens.aclose()
        self._engine.upstream_closed = True
        self._engine.upstream_closed_at = time.perf_counter()
        self._engine.upstream_closed_event.set()


def make_upstream_client(
    engine: FakeGPUEngine, *, close_checkpoints: bool = False
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose transport answers every request with a streamed engine."""

    async def handler(request: httpx.Request) -> httpx.Response:
        json.loads(request.content)
        stream = EngineByteStream(engine, close_checkpoints=close_checkpoints)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class ASGIConnection:
    """A scripted ASGI HTTP connection whose client side the test controls.

    ``receive()`` first replays the request body, then blocks until ``disconnect()``.
    ``send()`` records messages; after ``disconnect()`` it raises ``ConnectionClosed``
    (spec 2.4 servers) or drops silently (spec 2.3 servers such as Uvicorn 0.54.0).
    ``hold_sends()`` makes ``send()`` block, modelling a client that stopped reading.
    """

    def __init__(
        self,
        *,
        spec_version: str = "2.3",
        body_parts: tuple[bytes, ...] = (b'{"prompt": "hi"}',),
        body_complete: bool = True,
    ) -> None:
        self.spec_version = spec_version
        self._pending_body = list(body_parts)
        self._body_complete = body_complete
        self._closed = asyncio.Event()
        self._progress = asyncio.Event()
        self._send_gate = asyncio.Event()
        self._send_gate.set()
        self.send_blocked = asyncio.Event()
        self.sent: list[Message] = []
        self.body_chunks = 0
        self.dropped = 0
        self.disconnected_at: float | None = None

    @property
    def scope(self) -> Scope:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": self.spec_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/stream",
            "raw_path": b"/v1/stream",
            "query_string": b"",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 9),
            "server": ("127.0.0.1", 80),
        }

    async def receive(self) -> Message:
        if self._pending_body:
            part = self._pending_body.pop(0)
            more = bool(self._pending_body) or not self._body_complete
            return {"type": "http.request", "body": part, "more_body": more}
        await self._closed.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if not self._send_gate.is_set():
            self.send_blocked.set()
        await self._send_gate.wait()
        if self._closed.is_set():
            if self.spec_version >= "2.4":
                raise ConnectionClosed("send() on a closed ASGI HTTP connection")
            self.dropped += 1
            return
        self.sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.body_chunks += 1
        self._progress.set()

    def hold_sends(self) -> None:
        self._send_gate.clear()

    def disconnect(self) -> None:
        """The client closes its socket."""
        self.disconnected_at = time.perf_counter()
        self._closed.set()

    async def wait_for_body_chunks(self, count: int) -> None:
        while self.body_chunks < count:
            self._progress.clear()
            await self._progress.wait()

    def body_text(self) -> str:
        return b"".join(m.get("body", b"") for m in self.sent).decode("ascii")


@dataclass
class GatewayLog:
    """What the gateway itself concluded about each request."""

    outcomes: list[str] = field(default_factory=list[str])


async def read_request_body(receive: Receive) -> bytes | None:
    """Concatenate ``http.request`` bodies; ``None`` if ``http.disconnect`` came first."""
    parts: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        parts.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(parts)


async def wait_for_disconnect(receive: Receive) -> None:
    """Return once ``http.disconnect`` arrives; the request body must already be consumed."""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


async def stream_upstream_tokens(
    client: httpx.AsyncClient, *, shield_close: bool = True
) -> AsyncGenerator[bytes, None]:
    """Yield one SSE event per upstream token; release the upstream response on exit.

    The release runs under cancellation, so it is shielded: an ``anyio`` cancel scope
    re-delivers its cancellation at every checkpoint inside the cancelled region.
    ``shield_close=False`` exists only to demonstrate that failure.
    """
    request = client.build_request("POST", UPSTREAM_URL, json={"prompt": "hi", "stream": True})
    response = await client.send(request, stream=True)
    try:
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                yield f"{line}\n\n".encode("ascii")
    finally:
        if shield_close:
            with anyio.CancelScope(shield=True):
                await response.aclose()
        else:
            await response.aclose()


@asynccontextmanager
async def closing_tokens(
    tokens: AsyncGenerator[bytes, None], *, shield: bool
) -> AsyncGenerator[AsyncGenerator[bytes, None], None]:
    """``contextlib.aclosing`` whose ``aclose()`` may be shielded from a cancelled scope."""
    try:
        yield tokens
    finally:
        if shield:
            with anyio.CancelScope(shield=True):
                await tokens.aclose()
        else:
            await tokens.aclose()


async def start_sse(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream"),
                (b"cache-control", b"no-store"),
                (b"x-accel-buffering", b"no"),
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class StreamResult:
    completed: bool
    client_disconnected: bool
    chunks_sent: int


async def serve_stream(
    receive: Receive,
    send: Send,
    tokens: AsyncGenerator[bytes, None],
    *,
    shield_close: bool = True,
) -> StreamResult:
    """Race the token pump against ``http.disconnect``; whichever ends first cancels the other.

    A ``send()`` that raises ``OSError`` (spec 2.4 closed connection) is treated as a
    disconnect too, but it is only observed when there is something to send.
    """
    chunks = 0
    completed = False
    disconnected = False

    async def send_body(body: bytes, *, more_body: bool) -> bool:
        nonlocal disconnected
        try:
            await send({"type": "http.response.body", "body": body, "more_body": more_body})
        except OSError:
            disconnected = True
            return False
        return True

    async with anyio.create_task_group() as group:

        async def pump() -> None:
            nonlocal chunks, completed
            try:
                async with closing_tokens(tokens, shield=shield_close) as events:
                    async for event in events:
                        if not await send_body(event, more_body=True):
                            return
                        chunks += 1
                completed = await send_body(b"", more_body=False)
            finally:
                group.cancel_scope.cancel()

        async def watch() -> None:
            nonlocal disconnected
            await wait_for_disconnect(receive)
            disconnected = True
            group.cancel_scope.cancel()

        group.start_soon(pump)
        group.start_soon(watch)

    return StreamResult(completed, disconnected, chunks)


def watching_gateway(
    client: httpx.AsyncClient, log: GatewayLog, *, shield_close: bool = True
) -> ASGIApp:
    """The production pattern: body first, then race the stream against ``http.disconnect``."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = await read_request_body(receive)
        if body is None:
            log.outcomes.append("disconnected_before_upstream")
            return
        await start_sse(send)
        result = await serve_stream(
            receive,
            send,
            stream_upstream_tokens(client, shield_close=shield_close),
            shield_close=shield_close,
        )
        log.outcomes.append("completed" if result.completed else "client_disconnect")

    return app


def starlette_gateway(client: httpx.AsyncClient, log: GatewayLog) -> ASGIApp:
    """Delegate to ``StreamingResponse``: it only listens for disconnect on spec < 2.4."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = await read_request_body(receive)
        if body is None:
            log.outcomes.append("disconnected_before_upstream")
            return
        response = StreamingResponse(stream_upstream_tokens(client), media_type="text/event-stream")
        try:
            await response(scope, receive, send)
        except ClientDisconnect:
            log.outcomes.append("client_disconnect")
        else:
            log.outcomes.append("returned")

    return app


def buffering_gateway(client: httpx.AsyncClient, log: GatewayLog) -> ASGIApp:
    """The anti-pattern: a producer task drains the upstream into a queue nobody can stop."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = await read_request_body(receive)
        if body is None:
            log.outcomes.append("disconnected_before_upstream")
            return
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def producer() -> None:
            async for event in stream_upstream_tokens(client):
                queue.put_nowait(event)
            queue.put_nowait(None)

        task = asyncio.create_task(producer())
        await start_sse(send)
        try:
            while (event := await queue.get()) is not None:
                await send({"type": "http.response.body", "body": event, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            log.outcomes.append("completed")
        except OSError:
            log.outcomes.append("client_disconnect")
        finally:
            await asyncio.shield(task)

    return app
