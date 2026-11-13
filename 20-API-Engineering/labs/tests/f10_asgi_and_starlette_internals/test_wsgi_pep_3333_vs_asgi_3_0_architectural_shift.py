"""WSGI (PEP 3333) inside ASGI 3.0: threads per request, late disconnects, missing scopes."""

from __future__ import annotations

import importlib
import sys
import threading
import time
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any
from wsgiref.validate import validator

import anyio
import httpx
import pytest
from asgi_lifespan import LifespanManager
from asgi_lifespan._exceptions import LifespanNotSupported
from asgiref.wsgi import WsgiToAsgi
from starlette.exceptions import StarletteDeprecationWarning

from api_labs.f10_asgi_and_starlette_internals.wsgi_asgi_bridge import (
    NativeASGIApp,
    WSGIBridge,
    build_environ,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from types import ModuleType
    from wsgiref.types import StartResponse
    from wsgiref.types import WSGIEnvironment as Environ

    from starlette.types import ASGIApp, Message, Scope


def http_scope(path: str = "/", headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "server": ("test", 80),
        "client": ("127.0.0.1", 50000),
    }


class Recorder:
    """An ASGI `send` that keeps every message."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if "body" in m)


class OneRequest:
    """An ASGI `receive`: one empty `http.request`, then `http.disconnect` once `gone` is set."""

    def __init__(self) -> None:
        self.delivered = False
        self.gone = anyio.Event()

    async def __call__(self) -> Message:
        if not self.delivered:
            self.delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}


async def wait_until(predicate: Callable[[], bool]) -> None:
    # Polls limiter and app counters that expose no event to wait on.
    with anyio.fail_after(5):
        while not predicate():  # noqa: ASYNC110
            await anyio.sleep(0.001)


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def import_starlette_wsgi() -> ModuleType:
    sys.modules.pop("starlette.middleware.wsgi", None)
    with pytest.warns(StarletteDeprecationWarning, match="a2wsgi"):
        return importlib.import_module("starlette.middleware.wsgi")


# 1. Concurrency: WSGI needs a thread per in-flight request; ASGI needs a coroutine.


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_wsgi_long_polls_exhaust_a_two_thread_limiter_and_starve_health() -> None:
    release = threading.Event()

    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [("content-type", "text/plain")])
        if environ["PATH_INFO"] != "/stream":
            return [b"ok"]

        def long_poll() -> Iterator[bytes]:
            yield b"first"
            release.wait(timeout=10)  # Blocks the worker thread, as any WSGI wait must.
            yield b"last"

        return long_poll()

    limiter = anyio.CapacityLimiter(2)
    bridge = WSGIBridge(app, limiter=limiter)
    health = Recorder()
    try:
        async with anyio.create_task_group() as tg:
            for _ in range(2):
                tg.start_soon(bridge, http_scope("/stream"), OneRequest(), Recorder())
            await wait_until(lambda: limiter.borrowed_tokens == 2)
            tg.start_soon(bridge, http_scope("/health"), OneRequest(), health)
            await wait_until(lambda: limiter.statistics().tasks_waiting == 1)
            assert health.messages == []  # Queued for a thread, not served.
            release.set()
    finally:
        release.set()
    assert health.messages[0]["status"] == 200
    assert health.body == b"ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_native_asgi_holds_200_streams_on_one_thread_and_still_answers_health() -> None:
    app = NativeASGIApp()
    health = Recorder()
    async with anyio.create_task_group() as tg:
        for _ in range(200):
            tg.start_soon(app, http_scope("/stream"), OneRequest(), Recorder())
        await wait_until(lambda: app.open_streams == 200)
        await app(http_scope("/health"), OneRequest(), health)
        assert health.body == b"ok"
        app.release.set()
    assert app.open_streams == 0
    assert app.thread_ids == {threading.get_ident()}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgiref_wsgitoasgi_serialises_every_request_onto_one_thread() -> None:
    lock = threading.Lock()
    active = [0]
    peak = [0]
    thread_ids: set[int] = set()

    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            thread_ids.add(threading.get_ident())
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        start_response("200 OK", [("content-type", "text/plain")])
        return [b"ok"]

    async with client_for(wrap_with_asgiref(app)) as client, anyio.create_task_group() as tg:
        for _ in range(4):
            tg.start_soon(client.get, "/")
    assert peak[0] == 1  # thread_sensitive=True: asgiref's single-thread executor.
    assert len(thread_ids) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_runs_requests_in_parallel_up_to_the_limiter() -> None:
    barrier = threading.Barrier(3, timeout=5)
    thread_ids: set[int] = set()

    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        thread_ids.add(threading.get_ident())
        barrier.wait()  # Raises BrokenBarrierError unless 3 requests run at once.
        start_response("200 OK", [("content-type", "text/plain")])
        return [b"ok"]

    statuses: list[int] = []

    async def get(client: httpx.AsyncClient) -> None:
        statuses.append((await client.get("/")).status_code)

    bridge = WSGIBridge(app, limiter=anyio.CapacityLimiter(3))
    async with client_for(bridge) as client, anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(get, client)
    assert statuses == [200, 200, 200]
    assert len(thread_ids) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_upload_is_buffered_on_the_loop_without_borrowing_a_thread() -> None:
    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [("content-type", "application/octet-stream")])
        return [environ["wsgi.input"].read()]

    gate = anyio.Event()
    waiting = anyio.Event()
    chunks: list[Message] = [
        {"type": "http.request", "body": b"hello", "more_body": True},
        {"type": "http.request", "body": b"world", "more_body": False},
    ]

    async def trickle() -> Message:
        if len(chunks) == 1:
            waiting.set()
            await gate.wait()
        return chunks.pop(0)

    limiter = anyio.CapacityLimiter(1)
    sent = Recorder()
    async with anyio.create_task_group() as tg:
        tg.start_soon(WSGIBridge(app, limiter=limiter), http_scope("/upload"), trickle, sent)
        await waiting.wait()
        assert limiter.borrowed_tokens == 0  # A stalled client costs a coroutine only.
        gate.set()
    assert sent.body == b"helloworld"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_over_the_cap_gets_413_before_any_thread_or_app_call() -> None:
    called: list[bool] = []

    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        called.append(True)
        start_response("200 OK", [])
        return [b""]

    async with client_for(WSGIBridge(app, limiter=anyio.CapacityLimiter(1), max_body=8)) as c:
        response = await c.post("/", content=b"123456789")
    assert response.status_code == 413
    assert called == []


# 2. Disconnects: WSGI learns at its next write; ASGI hears it while idle.


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wsgi_sees_a_disconnect_only_when_its_next_chunk_fails_and_close_runs() -> None:
    produced: list[bytes] = []
    finalized: list[bool] = []

    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [("content-type", "text/plain")])

        def body() -> Iterator[bytes]:
            try:
                for part in (b"one", b"two", b"three"):
                    produced.append(part)
                    yield part
            finally:
                finalized.append(True)

        return body()

    bodies = [0]

    async def gone_after_first_chunk(message: Message) -> None:
        if message["type"] == "http.response.body":
            bodies[0] += 1
            if bodies[0] > 1:
                raise OSError("client disconnected")

    bridge = WSGIBridge(app, limiter=anyio.CapacityLimiter(1))
    with pytest.raises(OSError, match="client disconnected"):
        await bridge(http_scope(), OneRequest(), gone_after_first_chunk)
    assert produced == [b"one", b"two"]  # "two" was rendered for nobody.
    assert finalized == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_native_asgi_observes_http_disconnect_while_idle_without_writing() -> None:
    app = NativeASGIApp()
    receive = OneRequest()
    sent = Recorder()
    async with anyio.create_task_group() as tg:
        tg.start_soon(app, http_scope("/stream"), receive, sent)
        await wait_until(lambda: app.open_streams == 1)
        receive.gone.set()
    assert app.disconnects_seen == 1
    assert app.open_streams == 0
    assert sent.body == b"first"  # Stopped without attempting another write.


# 3. Scopes WSGI cannot express: websocket and lifespan.


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_refuses_websocket_by_closing_before_accept() -> None:
    sent = Recorder()
    bridge = WSGIBridge(lambda environ, start_response: [b""], limiter=anyio.CapacityLimiter(1))

    async def connect() -> Message:
        return {"type": "websocket.connect"}

    await bridge({"type": "websocket", "path": "/ws", "headers": []}, connect, sent)
    assert sent.messages == [{"type": "websocket.close", "code": 1000}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_native_asgi_accepts_and_echoes_a_websocket() -> None:
    incoming: list[Message] = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "hi"},
        {"type": "websocket.disconnect", "code": 1000},
    ]

    async def receive() -> Message:
        return incoming.pop(0)

    sent = Recorder()
    await NativeASGIApp()({"type": "websocket", "path": "/ws", "headers": []}, receive, sent)
    assert sent.messages == [
        {"type": "websocket.accept"},
        {"type": "websocket.send", "text": "hi"},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_runs_on_native_asgi_and_is_unsupported_through_the_bridge() -> None:
    native = NativeASGIApp()
    async with LifespanManager(native):
        assert native.started
        assert not native.stopped
    assert native.stopped

    bridge = WSGIBridge(lambda environ, start_response: [b""], limiter=anyio.CapacityLimiter(1))
    with pytest.raises(LifespanNotSupported):
        async with LifespanManager(bridge):
            pass


# 4. PEP 3333 rules that bridges get wrong.


def validated_app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"validated"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_environ_passes_wsgiref_validator() -> None:
    bridge = WSGIBridge(validator(validated_app), limiter=anyio.CapacityLimiter(1))
    async with client_for(bridge) as client:
        response = await client.get("/v?x=1")
    assert response.text == "validated"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_wsgimiddleware_fails_wsgiref_validator_on_integer_server_port() -> None:
    middleware = import_starlette_wsgi().WSGIMiddleware(validator(validated_app))
    async with client_for(middleware) as client:
        with pytest.raises(AssertionError, match="SERVER_PORT is not a string"):
            await client.get("/v")


class ClosingBody:
    """A WSGI iterable whose `close()` must be called by the gateway (PEP 3333)."""

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield b"body"

    def close(self) -> None:
        self.closed = True


def wrap_with_bridge(app: Callable[..., Iterable[bytes]]) -> Any:
    return WSGIBridge(app, limiter=anyio.CapacityLimiter(1))


def wrap_with_starlette(app: Callable[..., Iterable[bytes]]) -> Any:
    return import_starlette_wsgi().WSGIMiddleware(app)


def wrap_with_uvicorn(app: Callable[..., Iterable[bytes]]) -> Any:
    # Without a2wsgi installed, uvicorn.middleware.wsgi.WSGIMiddleware is this class.
    from uvicorn.middleware.wsgi import _WSGIMiddleware  # pyright: ignore[reportPrivateUsage]

    with pytest.warns(DeprecationWarning, match="a2wsgi"):
        return _WSGIMiddleware(app)


def wrap_with_asgiref(app: Callable[..., Iterable[bytes]]) -> Any:
    return WsgiToAsgi(app)  # type: ignore[no-untyped-call]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrap", "calls_close"),
    [
        pytest.param(wrap_with_bridge, True, id="lab-bridge"),
        pytest.param(wrap_with_starlette, False, id="starlette-1.10.0"),
        pytest.param(wrap_with_uvicorn, False, id="uvicorn-0.54.0"),
        pytest.param(wrap_with_asgiref, False, id="asgiref-3.13.0"),
    ],
)
async def test_which_bridges_call_the_iterable_close_that_pep_3333_requires(
    wrap: Callable[[Callable[..., Iterable[bytes]]], Any], calls_close: bool
) -> None:
    body = ClosingBody()

    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [("content-type", "text/plain")])
        return body

    async with client_for(wrap(app)) as client:
        response = await client.get("/")
    assert response.content == b"body"
    assert body.closed is calls_close


@pytest.mark.unit
def test_http2_cookie_crumbs_rejoin_with_semicolon_not_comma() -> None:
    crumbs = [(b"cookie", b"session=s1"), (b"cookie", b"csrf=t2")]
    starlette_environ = import_starlette_wsgi().build_environ(http_scope(headers=crumbs), b"")
    lab_environ = build_environ(http_scope(headers=crumbs), b"")

    def parse(header: str) -> dict[str, str]:
        jar: SimpleCookie = SimpleCookie()
        jar.load(header)
        return {name: morsel.value for name, morsel in jar.items()}

    assert starlette_environ["HTTP_COOKIE"] == "session=s1,csrf=t2"
    assert parse(starlette_environ["HTTP_COOKIE"]) == {"session": "s1,csrf=t2"}
    assert lab_environ["HTTP_COOKIE"] == "session=s1; csrf=t2"
    assert parse(lab_environ["HTTP_COOKIE"]) == {"session": "s1", "csrf": "t2"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delayed_headers_let_exc_info_replace_200_with_500() -> None:
    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [("content-type", "text/html")])

        def body() -> Iterator[bytes]:
            try:
                yield b""  # Nothing real yet, so no headers have gone out.
                raise RuntimeError("template failed")
            except RuntimeError:
                start_response("500 Internal Server Error", [], sys.exc_info())
                yield b"error page"

        return body()

    async with client_for(WSGIBridge(app, limiter=anyio.CapacityLimiter(1))) as client:
        response = await client.get("/")
    assert response.status_code == 500
    assert response.content == b"error page"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exc_info_after_headers_were_sent_reraises_the_original_error() -> None:
    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [("content-type", "text/html")])

        def body() -> Iterator[bytes]:
            try:
                yield b"<html>"
                raise RuntimeError("template failed")
            except RuntimeError:
                start_response("500 Internal Server Error", [], sys.exc_info())
                yield b"never sent"

        return body()

    sent = Recorder()
    with pytest.raises(RuntimeError, match="template failed"):
        await WSGIBridge(app, limiter=anyio.CapacityLimiter(1))(http_scope(), OneRequest(), sent)
    assert sent.messages[0]["status"] == 200
    assert sent.body == b"<html>"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_write_callable_output_precedes_the_returned_iterable() -> None:
    def legacy_app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        write = start_response("200 OK", [("content-type", "text/plain")])
        write(b"pushed-1 ")
        write(b"pushed-2 ")
        return [b"returned"]

    async with client_for(WSGIBridge(legacy_app, limiter=anyio.CapacityLimiter(1))) as client:
        response = await client.get("/")
    assert response.content == b"pushed-1 pushed-2 returned"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("X-Note", "a\r\nSet-Cookie: admin=1", "control character"),
        ("Connection", "close", "hop-by-hop"),
        ("Transfer-Encoding", "chunked", "hop-by-hop"),
    ],
)
async def test_start_response_rejects_crlf_and_hop_by_hop_headers(
    name: str, value: str, reason: str
) -> None:
    def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [(name, value)])
        return [b"x"]

    sent = Recorder()
    with pytest.raises(ValueError, match=reason):
        await WSGIBridge(app, limiter=anyio.CapacityLimiter(1))(http_scope(), OneRequest(), sent)
    assert sent.messages == []
