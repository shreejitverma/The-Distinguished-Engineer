"""BaseHTTPMiddleware versus pure ASGI middleware, measured in-process.

Every helper here drives raw ASGI callables so the lab can observe what an HTTP
server would see: each message, its order relative to the producer, the tasks
the event loop creates, and the exceptions that escape. Pinned to
starlette 1.10.0 and anyio 4.16.0.
"""

from __future__ import annotations

import asyncio
import selectors
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import anyio
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping

    from _typeshed import FileDescriptorLike
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_USER: ContextVar[str] = ContextVar("request_user", default="unset")
REQUEST_BAG: ContextVar[dict[str, str] | None] = ContextVar("request_bag", default=None)


# --- Drivers: a minimal in-process "server" -------------------------------------------


def http_scope(path: str = "/") -> Scope:
    """Build an HTTP scope; no `asgi.spec_version`, so apps must assume 2.0."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("127.0.0.1", 50000),
    }


async def drive(
    app: ASGIApp,
    scope: Scope | None = None,
    *,
    body: bytes = b"",
    log: list[str] | None = None,
    client_yields: int = 0,
) -> list[Message]:
    """Run one request and return every message the app sent to the server.

    `receive` delivers the body once, then blocks until the final body message
    has been sent and reports `http.disconnect`, like a server whose client
    closed after reading the response. `client_yields` makes each `send`
    yield to the event loop that many times, modelling a slow socket.
    """
    sent: list[Message] = []
    done = anyio.Event()
    body_delivered = False

    async def receive() -> Message:
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        for _ in range(client_yields):
            await asyncio.sleep(0)
        sent.append(message)
        if message["type"] == "http.response.body":
            if log is not None and message.get("body"):
                log.append(f"deliver:{message['body'].decode()}")
            if not message.get("more_body", False):
                if log is not None:
                    log.append("deliver:final")
                done.set()

    await app(scope if scope is not None else http_scope(), receive, send)
    return sent


async def count_tasks_created(run: Callable[[], Awaitable[object]]) -> int:
    """Count the asyncio tasks created while `run()` executes (task factory hook)."""
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    created = 0

    def factory(
        loop: asyncio.AbstractEventLoop,
        coro: Coroutine[Any, Any, Any],
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        nonlocal created
        created += 1
        return asyncio.Task(coro, loop=loop, **kwargs)

    loop.set_task_factory(factory)  # pyright: ignore[reportArgumentType]
    try:
        await run()
    finally:
        loop.set_task_factory(previous)
    return created


def max_lead(log: list[str]) -> int:
    """Largest number of chunks produced but not yet delivered at any point in `log`."""
    produced = delivered = lead = 0
    for event in log:
        if event.startswith("produce:"):
            produced += 1
        elif event.startswith("deliver:") and event != "deliver:final":
            delivered += 1
        lead = max(lead, produced - delivered)
    return lead


# --- Downstream applications ------------------------------------------------------------


async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Smallest realistic endpoint: a Starlette PlainTextResponse."""
    await PlainTextResponse("ok")(scope, receive, send)


async def set_user_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Report the inbound REQUEST_USER, then set it and fill the bag and request.state."""
    inbound = REQUEST_USER.get()
    REQUEST_USER.set("alice")
    bag = REQUEST_BAG.get()
    if bag is not None:
        bag["user"] = "alice"
    Request(scope).state.user = "alice"
    await PlainTextResponse(inbound)(scope, receive, send)


async def echo_body_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Read the request body through Starlette and echo it."""
    request = Request(scope, receive)
    await PlainTextResponse(await request.body())(scope, receive, send)


def streaming_app(chunks: int, log: list[str]) -> ASGIApp:
    """A StreamingResponse whose generator never awaits: a fast producer."""

    async def generate() -> AsyncIterator[bytes]:
        for index in range(chunks):
            log.append(f"produce:{index}")
            yield str(index).encode()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await StreamingResponse(generate())(scope, receive, send)

    return app


def failing_app(*, after_chunks: int | None) -> ASGIApp:
    """Raise ValueError before the response starts, or after `after_chunks` chunks."""

    async def generate() -> AsyncIterator[bytes]:
        for index in range(after_chunks or 0):
            yield str(index).encode()
        raise ValueError("boom mid-stream")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if after_chunks is None:
            raise ValueError("boom before start")
        await StreamingResponse(generate())(scope, receive, send)

    return app


def background_app(log: list[str]) -> ASGIApp:
    """A response with a BackgroundTask that records when it runs."""

    async def record() -> None:
        log.append("background")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok", background=BackgroundTask(record))(scope, receive, send)

    return app


async def trailers_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Raw ASGI app using the HTTP Trailers extension (`trailers: True` on start)."""
    await send({"type": "http.response.start", "status": 200, "headers": [], "trailers": True})
    await send({"type": "http.response.body", "body": b"payload", "more_body": False})
    await send({"type": "http.response.trailers", "headers": [(b"x-checksum", b"abc")]})


async def zerocopy_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Raw ASGI app using the zero-copy send extension message."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.zerocopysend", "file": None, "more_body": False})


# --- Middleware under test --------------------------------------------------------------


class BaseHTTPPassthrough(BaseHTTPMiddleware):
    """The cheapest possible BaseHTTPMiddleware: it only calls call_next."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        return await call_next(request)


class PurePassthrough:
    """The pure ASGI equivalent: one extra await, no task, no stream."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


class BaseHTTPContextProbe(BaseHTTPMiddleware):
    """Record what the dispatch task sees after call_next returns."""

    def __init__(self, app: ASGIApp, seen: list[str]) -> None:
        super().__init__(app)
        self.seen = seen

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        REQUEST_USER.set("from-dispatch")
        bag: dict[str, str] = {}
        token = REQUEST_BAG.set(bag)
        try:
            response = await call_next(request)
        finally:
            REQUEST_BAG.reset(token)
        self.seen.append(f"var={REQUEST_USER.get()}")
        self.seen.append(f"bag={bag.get('user', 'unset')}")
        self.seen.append(f"state={getattr(request.state, 'user', 'unset')}")
        return response


class PureContextProbe:
    """Record REQUEST_USER after the downstream app returns, in the same task."""

    def __init__(self, app: ASGIApp, seen: list[str]) -> None:
        self.app = app
        self.seen = seen

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)
        if scope["type"] == "http":
            self.seen.append(f"var={REQUEST_USER.get()}")


class CatchingMiddleware(BaseHTTPMiddleware):
    """Turn a downstream ValueError into a 500 inside dispatch."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except ValueError as exc:
            return PlainTextResponse(f"caught {type(exc).__name__}", status_code=500)


class RaiseInDispatch(BaseHTTPMiddleware):
    """Raise from dispatch while the downstream task is still blocked on send."""

    def __init__(self, app: ASGIApp, exc: Exception) -> None:
        super().__init__(app)
        self.exc = exc

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        await call_next(request)
        raise self.exc


class NaiveSpawningMiddleware:
    """Run the downstream app as a child of a plain anyio task group (no collapsing)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def run_app() -> None:
            await self.app(scope, receive, send)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_app)


class BodyInspectingMiddleware(BaseHTTPMiddleware):
    """The common anti-pattern: drain body_iterator to inspect the response body."""

    def __init__(self, app: ASGIApp, log: list[str]) -> None:
        super().__init__(app)
        self.log = log

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        body_iterator: AsyncIterator[bytes] = vars(response)["body_iterator"]
        body = b"".join([chunk async for chunk in body_iterator])
        self.log.append(f"inspected:{len(body)}")
        return Response(body, status_code=response.status_code, headers=dict(response.headers))


class PureBodyObserver:
    """The pure ASGI equivalent: observe each body chunk as it passes through send."""

    def __init__(self, app: ASGIApp, log: list[str]) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        seen = 0

        async def observe(message: Message) -> None:
            nonlocal seen
            if message["type"] == "http.response.body":
                seen += len(message.get("body", b""))
                if not message.get("more_body", False):
                    self.log.append(f"inspected:{seen}")
            await send(message)

        await self.app(scope, receive, observe)


class ReadBodyInDispatch(BaseHTTPMiddleware):
    """Read the whole request body in dispatch before calling the endpoint."""

    def __init__(self, app: ASGIApp, log: list[str]) -> None:
        super().__init__(app)
        self.log = log

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        self.log.append(f"dispatch-read:{len(await request.body())}")
        return await call_next(request)


class LegacyQueueMiddleware:
    """Simplified replica of starlette 0.14.2 call_next: an unbounded asyncio.Queue."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        queue: asyncio.Queue[Message | None] = asyncio.Queue()

        async def run_app() -> None:
            try:
                await self.app(scope, receive, queue.put)
            finally:
                await queue.put(None)

        task = asyncio.get_running_loop().create_task(run_app())
        while (message := await queue.get()) is not None:
            await send(message)
        await task


def stack(app: ASGIApp, layer: Callable[[ASGIApp], ASGIApp], depth: int) -> ASGIApp:
    """Wrap `app` in `depth` instances of `layer`."""
    for _ in range(depth):
        app = layer(app)
    return app


# --- Event-loop iterations and throughput --------------------------------------------


class PollCountingSelector(selectors.BaseSelector):
    """A selector that never blocks and counts polls: one poll per loop iteration.

    asyncio's selector loop calls `select()` exactly once per `_run_once`, so
    `polls` is the number of event-loop iterations. Nothing here performs real
    I/O, so returning no events is correct; it also removes the poll syscall
    from timing runs, leaving only interpreter work.
    """

    def __init__(self) -> None:
        self.polls = 0
        self._keys: dict[int, selectors.SelectorKey] = {}

    @staticmethod
    def _fd(fileobj: FileDescriptorLike) -> int:
        return fileobj if isinstance(fileobj, int) else fileobj.fileno()

    def register(
        self, fileobj: FileDescriptorLike, events: int, data: Any = None
    ) -> selectors.SelectorKey:
        key = selectors.SelectorKey(fileobj, self._fd(fileobj), events, data)
        self._keys[key.fd] = key
        return key

    def unregister(self, fileobj: FileDescriptorLike) -> selectors.SelectorKey:
        return self._keys.pop(self._fd(fileobj))

    def get_key(self, fileobj: FileDescriptorLike) -> selectors.SelectorKey:
        return self._keys[self._fd(fileobj)]

    def get_map(self) -> Mapping[FileDescriptorLike, selectors.SelectorKey]:
        mapping: dict[FileDescriptorLike, selectors.SelectorKey] = {
            key.fileobj: key for key in self._keys.values()
        }
        return mapping

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        self.polls += 1
        return []


def loop_iterations(app: ASGIApp, requests: int = 1) -> int:
    """Event-loop iterations needed to serve `requests` sequential requests via `app`."""
    selector = PollCountingSelector()
    loop = asyncio.SelectorEventLoop(selector)
    try:

        async def serve() -> int:
            await drive(app)  # warm-up request: lazy imports and first-call caches
            before = selector.polls
            for _ in range(requests):
                await drive(app)
            return selector.polls - before

        return loop.run_until_complete(serve())
    finally:
        loop.close()


def cpu_microseconds_per_request(app: ASGIApp, requests: int, repeats: int = 3) -> float:
    """Best-of-`repeats` mean process CPU microseconds per request on the poll-free loop.

    CPU time (not wall time) on a loop with no poll syscall isolates interpreter
    work from scheduler noise on a loaded machine.
    """
    loop = asyncio.SelectorEventLoop(PollCountingSelector())
    try:

        async def serve() -> float:
            for _ in range(min(500, requests)):
                await drive(app)
            best = float("inf")
            for _ in range(repeats):
                start = time.process_time()
                for _ in range(requests):
                    await drive(app)
                best = min(best, (time.process_time() - start) / requests * 1e6)
            return best

        return loop.run_until_complete(serve())
    finally:
        loop.close()


def tasks_per_request(app: ASGIApp) -> int:
    """asyncio tasks created while serving one request through `app`."""
    loop = asyncio.SelectorEventLoop(PollCountingSelector())
    try:
        return loop.run_until_complete(count_tasks_created(lambda: drive(app)))
    finally:
        loop.close()


def benchmark_stacks() -> dict[str, ASGIApp]:
    """The middleware stacks the note compares, all over `ok_app`."""
    return {
        "bare": ok_app,
        "pure x1": stack(ok_app, PurePassthrough, 1),
        "basehttp x1": stack(ok_app, BaseHTTPPassthrough, 1),
        "pure x5": stack(ok_app, PurePassthrough, 5),
        "basehttp x5": stack(ok_app, BaseHTTPPassthrough, 5),
    }


def benchmark_report(requests: int = 20000) -> list[tuple[str, int, int, float]]:
    """Per stack: loop iterations, tasks created, and CPU microseconds per request."""
    return [
        (
            name,
            loop_iterations(app),
            tasks_per_request(app),
            round(cpu_microseconds_per_request(app, requests), 2),
        )
        for name, app in benchmark_stacks().items()
    ]
