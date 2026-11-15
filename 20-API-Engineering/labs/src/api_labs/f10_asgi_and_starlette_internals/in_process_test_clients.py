"""A probe ASGI app that records where and how an in-process test client runs it.

The same Starlette app is driven by `httpx.ASGITransport` (the app runs inside the
test's own task and event loop) and by `starlette.testclient.TestClient` (the app runs
on an AnyIO blocking-portal event loop in a second thread). Every route records an
observable fact (thread, loop, context variable, stream progress, lifespan state) so a
test can prove which execution model it got.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

REQUEST_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("request_tag", default="unset")
"""Set by the test before a request, read and overwritten by the `/where` route."""

POLL_WAIT_SECONDS = 0.001
"""Long-poll timeout: long enough that `Event.wait()` binds the event to the running loop."""


@dataclass
class Sighting:
    """Where one request ran, and what context it inherited."""

    thread_id: int
    loop: asyncio.AbstractEventLoop
    tag_seen: str


@dataclass
class Probe:
    """Shared, per-test record of everything the app observed."""

    sightings: list[Sighting] = field(default_factory=list[Sighting])
    lifespan_loops: list[asyncio.AbstractEventLoop] = field(
        default_factory=list[asyncio.AbstractEventLoop]
    )
    stream_events: list[str] = field(default_factory=list[str])
    stream_resumed: threading.Event = field(default_factory=threading.Event)
    # Created outside any loop, exactly like a module-level or app-construction-time
    # primitive; asyncio binds it to the first loop that blocks on it.
    changed: asyncio.Event = field(default_factory=asyncio.Event)


def build_app(probe: Probe) -> Starlette:
    """Build a Starlette app whose routes report their execution environment into `probe`."""

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[Mapping[str, Any]]:
        probe.lifespan_loops.append(asyncio.get_running_loop())
        yield {"counter": 0}

    async def where(_request: Request) -> JSONResponse:
        seen = REQUEST_TAG.get()
        probe.sightings.append(
            Sighting(threading.get_ident(), asyncio.get_running_loop(), tag_seen=seen)
        )
        REQUEST_TAG.set("written-by-app")
        return JSONResponse({"tag_seen": seen})

    async def poll(_request: Request) -> Response:
        try:
            await asyncio.wait_for(probe.changed.wait(), timeout=POLL_WAIT_SECONDS)
        except TimeoutError:
            return Response(status_code=204)
        return Response(status_code=200)

    async def stream(request: Request) -> StreamingResponse:
        hold = request.query_params.get("hold") == "1"

        async def body() -> AsyncIterator[bytes]:
            probe.stream_events.append("start")
            yield b"first"
            probe.stream_events.append("resumed")
            probe.stream_resumed.set()
            if hold:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    probe.stream_events.append("cancelled")
                    raise
            yield b"second"
            probe.stream_events.append("finished")

        return StreamingResponse(body(), media_type="text/plain")

    async def slow(_request: Request) -> Response:
        await asyncio.sleep(0.05)
        return Response(b"late", media_type="text/plain")

    async def state(request: Request) -> JSONResponse:
        if "counter" not in request.scope.get("state", {}):
            return JSONResponse({"error": "lifespan state missing"}, status_code=503)
        request.state.counter += 1
        return JSONResponse({"counter": request.state.counter})

    async def scope_view(request: Request) -> JSONResponse:
        scope = request.scope
        return JSONResponse(
            {
                "asgi": scope.get("asgi"),
                "client": list(scope["client"]),
                "server": list(scope["server"]),
                "extensions": sorted(scope.get("extensions", {})),
                "has_state": "state" in scope,
                "http_version": scope["http_version"],
                "user_agent": request.headers.get("user-agent"),
            }
        )

    async def login(_request: Request) -> Response:
        response = Response(status_code=204)
        response.set_cookie("sid", "abc", secure=True, httponly=True)
        return response

    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse({"sid": request.cookies.get("sid")})

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/where", where),
            Route("/poll", poll),
            Route("/stream", stream),
            Route("/slow", slow),
            Route("/state", state),
            Route("/scope", scope_view),
            Route("/login", login, methods=["POST"]),
            Route("/whoami", whoami),
        ],
    )


@dataclass
class ReceiveRecorder:
    """Raw ASGI app that records every `http.request` event it receives, then replies 200."""

    events: list[tuple[bytes, bool]] = field(default_factory=list[tuple[bytes, bool]])
    raw_messages: list[Message] = field(default_factory=list["Message"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")
        while True:
            message = await receive()
            self.raw_messages.append(message)
            more_body = bool(message.get("more_body", False))
            self.events.append((bytes(message.get("body", b"")), more_body))
            if not more_body:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def crash_before_start(scope: Scope, receive: Receive, send: Send) -> None:
    """Raw ASGI app that raises before sending `http.response.start`."""
    raise ValueError("boom before response start")


async def no_response(scope: Scope, receive: Receive, send: Send) -> None:
    """Raw ASGI app that returns without sending anything: a contract violation."""
    return None
