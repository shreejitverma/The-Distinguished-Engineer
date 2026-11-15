"""Uvicorn 0.54.0 internals: Server lifecycle, H11Protocol vs HttpToolsProtocol, FlowControl.

Unit tests drive the real protocol classes over an in-memory transport; socket tests run the
real ``Server.serve`` and real event-loop transports on ``127.0.0.1:0``.
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING, Any, cast

import h11
import pytest
import uvloop
from h11._connection import DEFAULT_MAX_INCOMPLETE_EVENT_SIZE
from uvicorn.config import Config
from uvicorn.protocols.http.flow_control import HIGH_WATER_LIMIT
from uvicorn.server import Server, ServerState

from api_labs.f12_asgi_servers_and_async_runtime.uvicorn_internals import (
    ProtocolKind,
    build_config,
    get_request,
    open_connection,
    response_head,
    settle,
    status_codes,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

Scope = dict[str, Any]
Message = dict[str, Any]

KINDS: tuple[ProtocolKind, ...] = ("h11", "httptools")

SERVICE_UNAVAILABLE_WIRE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"content-type: text/plain; charset=utf-8\r\n"
    b"content-length: 19\r\n"
    b"connection: close\r\n"
    b"\r\n"
    b"Service Unavailable"
)


async def respond(send: Callable[[Message], Awaitable[None]], body: bytes) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body, "more_body": False})


# --limit-concurrency ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("limit", "expected"), [(1, 503), (2, 200)])
async def test_limit_concurrency_counts_the_requesting_connection_itself(
    kind: ProtocolKind, limit: int, expected: int
) -> None:
    calls: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        calls.append(scope["path"])
        await respond(send, b"ok")

    state = ServerState()
    config = build_config(app, kind, limit_concurrency=limit)
    conn = open_connection(config, state, asyncio.get_running_loop())
    conn.feed(get_request())
    await settle()

    assert status_codes(conn.transport.output) == [expected]
    if expected == 503:
        # A lone client on an idle server is rejected: connection_made already added it.
        assert bytes(conn.transport.output) == SERVICE_UNAVAILABLE_WIRE
        assert calls == []
        assert conn.transport.closed
    else:
        assert calls == ["/"]
        assert not conn.transport.closed


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_limit_concurrency_counts_tasks_whose_clients_already_left(
    kind: ProtocolKind,
) -> None:
    gate = asyncio.Event()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await gate.wait()
        await respond(send, b"late")

    loop = asyncio.get_running_loop()
    state = ServerState()
    config = build_config(app, kind, limit_concurrency=2)
    for path in ("/a", "/b"):
        abandoned = open_connection(config, state, loop)
        abandoned.feed(get_request(path))
        await settle()
        abandoned.lose()  # client hangs up; its ASGI task keeps running

    assert (len(state.connections), len(state.tasks)) == (0, 2)
    late = open_connection(config, state, loop)
    late.feed(get_request("/late"))
    await settle()
    assert status_codes(late.transport.output) == [503]

    gate.set()  # abandoned tasks finish; their send() calls are dropped silently
    await settle()
    assert len(state.tasks) == 0
    fresh = open_connection(config, state, loop)
    fresh.feed(get_request("/fresh"))
    await settle()
    assert status_codes(fresh.transport.output) == [200]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_unfinished_request_heads_hold_concurrency_slots_with_no_timer(
    kind: ProtocolKind,
) -> None:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await respond(send, b"ok")

    loop = asyncio.get_running_loop()
    state = ServerState()
    config = build_config(app, kind, limit_concurrency=3, timeout_keep_alive=0)
    idlers = [open_connection(config, state, loop) for _ in range(2)]
    idlers[0].feed(b"GET / HTTP/1.1\r\nHost: te")  # a slowloris-style partial head
    await settle()

    # The keep-alive timer is armed only in on_response_complete, so nothing reaps these.
    assert all(c.protocol.timeout_keep_alive_task is None for c in idlers)
    assert not any(c.transport.closed for c in idlers)
    victim = open_connection(config, state, loop)
    victim.feed(get_request())
    await settle()
    assert status_codes(victim.transport.output) == [503]


# FlowControl: read side --------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_request_body_beyond_high_water_limit_pauses_reading(kind: ProtocolKind) -> None:
    gate = asyncio.Event()
    received: list[int] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await gate.wait()  # the app is busy and has not called receive() yet
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message["body"]
            more_body = message["more_body"]
        received.append(len(body))
        await respond(send, b"stored")

    size = HIGH_WATER_LIMIT + 1
    head = f"POST /upload HTTP/1.1\r\nHost: test\r\nContent-Length: {size}\r\n\r\n".encode()
    conn = open_connection(build_config(app, kind), ServerState(), asyncio.get_running_loop())

    conn.feed(head + b"x" * HIGH_WATER_LIMIT)  # exactly 65536 buffered: not strictly above
    await settle()
    assert conn.transport.pause_reading_calls == 0

    conn.feed(b"x")  # 65537 buffered bytes > HIGH_WATER_LIMIT
    await settle()
    assert conn.transport.pause_reading_calls == 1
    assert conn.protocol.flow.read_paused

    gate.set()  # receive() calls flow.resume_reading() before it waits
    await settle()
    assert conn.transport.resume_reading_calls >= 1
    assert received == [size]
    assert status_codes(conn.transport.output) == [200]


# FlowControl: write side -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_pause_writing_parks_send_until_resume_writing(kind: ProtocolKind) -> None:
    sent: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        sent.append("start")
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        sent.append("body")

    conn = open_connection(build_config(app, kind), ServerState(), asyncio.get_running_loop())
    conn.protocol.pause_writing()  # what the transport calls above its write high watermark
    conn.feed(get_request())
    await settle()
    assert sent == []
    assert conn.transport.output == b""

    conn.protocol.resume_writing()  # transport drained to its low watermark
    await settle()
    assert sent == ["start", "body"]
    assert status_codes(conn.transport.output) == [200]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_disconnect_while_write_paused_releases_send_without_raising(
    kind: ProtocolKind,
) -> None:
    outcome: list[str] = []
    versions: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        versions.append(scope["asgi"]["spec_version"])
        try:
            await respond(send, b"lost")
        except OSError:
            outcome.append("raised")
        else:
            outcome.append("returned")

    conn = open_connection(build_config(app, kind), ServerState(), asyncio.get_running_loop())
    conn.protocol.pause_writing()
    conn.feed(get_request())
    await settle()
    conn.lose()  # connection_lost() calls flow.resume_writing(), waking the parked send()
    await settle()

    assert versions == ["2.3"]  # the ASGI 2.4 "send raises OSError" rule is not claimed
    assert outcome == ["returned"]
    assert conn.transport.output == b""


# Pipelining ----------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_pipelined_requests_are_served_one_at_a_time_in_order(kind: ProtocolKind) -> None:
    gates = {"/1": asyncio.Event(), "/2": asyncio.Event()}
    started: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        started.append(scope["path"])
        await gates[scope["path"]].wait()
        await respond(send, scope["path"].encode())

    conn = open_connection(build_config(app, kind), ServerState(), asyncio.get_running_loop())
    conn.feed(get_request("/1") + get_request("/2"))  # both requests in one TCP segment
    await settle()
    assert started == ["/1"]
    assert conn.transport.pause_reading_calls == 1

    gates["/2"].set()  # releasing the second request first changes nothing
    await settle()
    assert started == ["/1"]

    gates["/1"].set()
    await settle()
    assert started == ["/1", "/2"]
    output = bytes(conn.transport.output)
    assert status_codes(output) == [200, 200]
    assert output.index(b"/1") < output.index(b"/2")


# Protocol differences ------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "chunked"), [("h11", False), ("httptools", True)])
async def test_http_1_0_request_gets_chunked_framing_only_from_httptools(
    kind: ProtocolKind, chunked: bool
) -> None:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await respond(send, b"hi")  # no Content-Length: the server must choose framing

    conn = open_connection(build_config(app, kind), ServerState(), asyncio.get_running_loop())
    conn.feed(get_request("/", version="1.0"))
    await settle()

    head = response_head(conn.transport.output)
    assert (b"transfer-encoding: chunked" in head) is chunked
    assert b"connection: close" in head
    assert conn.transport.closed  # neither implementation keeps an HTTP/1.0 connection alive


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "max_size", "expected"),
    [("h11", None, [400]), ("h11", 64 * 1024, []), ("httptools", None, [])],
)
async def test_incomplete_request_head_is_capped_only_by_h11(
    kind: ProtocolKind, max_size: int | None, expected: list[int]
) -> None:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await respond(send, b"unreachable")

    assert DEFAULT_MAX_INCOMPLETE_EVENT_SIZE == 16 * 1024
    overrides: dict[str, Any] = {}
    if max_size is not None:
        overrides["h11_max_incomplete_event_size"] = max_size
    config = build_config(app, kind, **overrides)
    conn = open_connection(config, ServerState(), asyncio.get_running_loop())
    conn.feed(b"GET / HTTP/1.1\r\nHost: test\r\nX-Pad: " + b"a" * (32 * 1024))  # no CRLFCRLF
    await settle()

    assert status_codes(conn.transport.output) == expected
    assert conn.transport.closed is bool(expected)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("keep_alive", "closed"), [(0, True), (5, False)])
async def test_keep_alive_timer_is_armed_after_the_response(
    kind: ProtocolKind, keep_alive: int, closed: bool
) -> None:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await respond(send, b"ok")

    loop = asyncio.get_running_loop()
    config = build_config(app, kind, timeout_keep_alive=keep_alive)
    conn = open_connection(config, ServerState(), loop)
    assert conn.protocol.timeout_keep_alive_task is None  # nothing armed before a response
    conn.feed(get_request())
    await settle()

    assert status_codes(conn.transport.output) == [200]
    assert conn.transport.closed is closed
    if not closed:
        timer = conn.protocol.timeout_keep_alive_task
        assert timer is not None
        assert 4.5 < timer.when() - loop.time() <= keep_alive
        conn.feed(get_request("/again"))  # new bytes cancel the idle timer
        assert conn.protocol.timeout_keep_alive_task is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "close_header"), [("h11", False), ("httptools", True)])
async def test_shutdown_closes_idle_connections_and_lets_inflight_finish(
    kind: ProtocolKind, close_header: bool
) -> None:
    gate = asyncio.Event()

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await gate.wait()
        await respond(send, b"drained")

    loop = asyncio.get_running_loop()
    state = ServerState()
    config = build_config(app, kind)
    idle = open_connection(config, state, loop)
    busy = open_connection(config, state, loop)
    busy.feed(get_request())
    await settle()

    for connection in list(state.connections):  # the loop in Server.shutdown
        connection.shutdown()
    assert idle.transport.closed
    assert not busy.transport.closed

    gate.set()
    await settle()
    assert status_codes(busy.transport.output) == [200]
    assert busy.transport.closed
    assert (b"connection: close" in response_head(busy.transport.output)) is close_header


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_malformed_request_line_gets_400_and_close(kind: ProtocolKind) -> None:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await respond(send, b"unreachable")

    conn = open_connection(build_config(app, kind), ServerState(), asyncio.get_running_loop())
    conn.feed(b"GET / HTTP/1.1\r\nBad Header\r\n\r\n")
    await settle()
    assert status_codes(conn.transport.output) == [400]
    assert conn.transport.closed


# Server lifecycle without sockets ----------------------------------------------------------


async def _noop_app(scope: Scope, receive: Any, send: Any) -> None:
    await respond(send, b"")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_tick_refreshes_default_headers_every_tenth_tick_and_enforces_max_requests() -> (
    None
):
    server = Server(build_config(_noop_app, "h11", limit_max_requests=3))
    assert server.limit_max_requests == 3  # jitter 0

    server.server_state.total_requests = 2
    assert await server.on_tick(0) is False
    names = [name for name, _ in server.server_state.default_headers]
    assert names == [b"date", b"server"]

    server.server_state.default_headers = []
    assert await server.on_tick(1) is False
    assert server.server_state.default_headers == []  # only counter % 10 == 0 refreshes

    server.server_state.total_requests = 3
    assert await server.on_tick(2) is True


@pytest.mark.unit
def test_max_requests_jitter_is_drawn_once_per_server() -> None:
    config = build_config(_noop_app, "h11", limit_max_requests=100, limit_max_requests_jitter=50)
    draws = {Server(config).limit_max_requests for _ in range(200)}
    assert all(d is not None and 100 <= d <= 150 for d in draws)
    assert len(draws) > 1
    server = Server(config)
    assert server.limit_max_requests == server.limit_max_requests  # cached_property


@pytest.mark.unit
def test_second_sigint_forces_exit_but_repeated_sigterm_does_not() -> None:
    server = Server(build_config(_noop_app, "h11"))
    server.handle_exit(signal.SIGTERM, None)
    server.handle_exit(signal.SIGTERM, None)
    assert (server.should_exit, server.force_exit) == (True, False)
    server.handle_exit(signal.SIGINT, None)
    assert (server.should_exit, server.force_exit) == (True, True)


# Real Server.serve() and real transports on loopback -----------------------------------------


async def _start(server: Server) -> tuple[asyncio.Task[None], int]:
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(5):
        while not server.started:  # noqa: ASYNC110 - Server exposes a flag, not an event
            await asyncio.sleep(0.01)
    sockets = server.servers[0].sockets
    return task, int(sockets[0].getsockname()[1])


async def _exchange(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    async with asyncio.timeout(5):
        data = await reader.read()  # until the server closes
    writer.close()
    await writer.wait_closed()
    return data


@pytest.mark.socket
@pytest.mark.asyncio
async def test_serve_orders_lifespan_startup_requests_and_lifespan_shutdown() -> None:
    events: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    events.append("startup")
                    await send({"type": "lifespan.startup.complete"})
                else:
                    events.append("shutdown")
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        events.append(f"http {scope['path']}")
        await respond(send, b"pong")

    config = Config(app=app, host="127.0.0.1", port=0, lifespan="on", log_level="critical")
    server = Server(config)
    task, port = await _start(server)
    response = await _exchange(port, b"GET /ping HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
    assert events == ["startup", "http /ping"]

    server.should_exit = True  # what handle_exit sets on SIGTERM
    await asyncio.wait_for(task, 5)
    assert events == ["startup", "http /ping", "shutdown"]
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")


@pytest.mark.socket
@pytest.mark.asyncio
async def test_graceful_shutdown_timeout_cancels_inflight_task_and_client_gets_500() -> None:
    started = asyncio.Event()
    cancel_messages: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()  # never finishes on its own
        except asyncio.CancelledError as exc:
            cancel_messages.append(str(exc.args[0]))
            raise

    config = Config(
        app=app,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        log_level="critical",
        timeout_graceful_shutdown=1,
    )
    server = Server(config)
    task, port = await _start(server)
    client = asyncio.create_task(_exchange(port, b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"))
    async with asyncio.timeout(5):
        await started.wait()

    server.should_exit = True
    await asyncio.wait_for(task, 5)
    response = await asyncio.wait_for(client, 5)

    assert cancel_messages == ["Task cancelled, timeout graceful shutdown exceeded"]
    # run_asgi catches BaseException, so the cancelled request still gets a 500 before close.
    assert response.startswith(b"HTTP/1.1 500 Internal Server Error\r\n")
    assert response.endswith(b"Internal Server Error")


class _Capture(asyncio.Protocol):
    def __init__(self, limits: asyncio.Future[tuple[int, int]]) -> None:
        self.limits = limits

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # uvloop's TCPTransport is not an asyncio.WriteTransport subclass; it duck-types one.
        self.limits.set_result(cast("asyncio.WriteTransport", transport).get_write_buffer_limits())


async def _probe_write_limits() -> tuple[int, int]:
    loop = asyncio.get_running_loop()
    limits: asyncio.Future[tuple[int, int]] = loop.create_future()
    server = await loop.create_server(lambda: _Capture(limits), "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    low_high = await asyncio.wait_for(limits, 5)
    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()
    return low_high


@pytest.mark.socket
@pytest.mark.parametrize(
    ("loop_name", "expected"),
    [("asyncio", (16 * 1024, 64 * 1024)), ("uvloop", (16, 64 * 1024))],
)
def test_default_transport_write_watermarks_differ_between_loops(
    loop_name: str, expected: tuple[int, int]
) -> None:
    factory: Callable[[], asyncio.AbstractEventLoop] = (
        asyncio.new_event_loop if loop_name == "asyncio" else uvloop.new_event_loop
    )
    with asyncio.Runner(loop_factory=factory) as runner:
        low_high = runner.run(_probe_write_limits())
    assert low_high == expected  # (low, high) from get_write_buffer_limits()


@pytest.mark.unit
def test_h11_server_reports_paused_for_a_buffered_second_request() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(get_request("/1") + get_request("/2"))
    assert isinstance(conn.next_event(), h11.Request)
    assert isinstance(conn.next_event(), h11.EndOfMessage)
    assert conn.next_event() is h11.PAUSED  # H11Protocol.handle_events -> flow.pause_reading()
