"""HTTP/1.1 pipelining order, head-of-line blocking, and HTTP/2 out-of-order completion."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

import h11
import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f01_http_semantics_and_wire_protocols.pipelining import (
    Schedule,
    completion_times,
    h2_exchange,
    nearest_rank,
    serve_pipelined_h11,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

GET_A = b"GET /a HTTP/1.1\r\nHost: t\r\n\r\n"
GET_B = b"GET /b HTTP/1.1\r\nHost: t\r\n\r\n"
GET_C = b"GET /c HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"

# One burst of 100 requests: one 200 ms request, ninety-nine 2 ms requests.
SLOW_FIRST = (200, *([2] * 99))


@pytest.mark.unit
def test_h11_server_releases_pipelined_requests_one_cycle_at_a_time() -> None:
    trace = serve_pipelined_h11(GET_A + GET_B + GET_C, lambda request: request.target)

    assert trace.dispatch_order == (b"/a", b"/b", b"/c")
    # After answering /a, h11 reports PAUSED with all of /b and /c still buffered;
    # after /b, it is PAUSED again with /c buffered. /c carries close, so no third pause.
    assert trace.paused_with_buffered_bytes == (len(GET_B + GET_C), len(GET_C))
    bodies = [trace.wire_response.index(target) for target in (b"/a", b"/b", b"/c")]
    assert bodies == sorted(bodies)
    assert trace.wire_response.endswith(b"\r\n\r\n/c")


@pytest.mark.unit
def test_h11_keep_alive_burst_ends_idle_without_a_trailing_pause() -> None:
    trace = serve_pipelined_h11(GET_A + GET_B, lambda request: request.target)

    assert trace.dispatch_order == (b"/a", b"/b")
    assert trace.paused_with_buffered_bytes == (len(GET_B),)
    assert trace.wire_response.count(b"HTTP/1.1 200 \r\n") == 2


@pytest.mark.unit
def test_h11_server_rejects_pipelined_request_with_unhandled_body() -> None:
    wire = b"POST /p HTTP/1.1\r\nHost: t\r\nContent-Length: 2\r\n\r\nhi" + GET_A
    with pytest.raises(ValueError, match="request bodies are out of scope"):
        serve_pipelined_h11(wire, lambda request: b"")


@pytest.mark.unit
def test_h11_client_refuses_to_pipeline_a_second_request() -> None:
    client = h11.Connection(h11.CLIENT)
    client.send(h11.Request(method="GET", target="/a", headers=[("Host", "t")]))
    client.send(h11.EndOfMessage())
    assert client.our_state is h11.DONE
    with pytest.raises(h11.LocalProtocolError, match="role=CLIENT and state=DONE"):
        client.send(h11.Request(method="GET", target="/b", headers=[("Host", "t")]))


@pytest.mark.unit
def test_serial_pipeline_turns_one_slow_response_into_everyone_s_tail_latency() -> None:
    serial = completion_times(SLOW_FIRST, Schedule.SERIAL)
    multiplexed = completion_times(SLOW_FIRST, Schedule.MULTIPLEXED)

    assert serial[:3] == (200, 202, 204)
    assert (nearest_rank(serial, 50), nearest_rank(serial, 99)) == (298, 396)
    assert (nearest_rank(multiplexed, 50), nearest_rank(multiplexed, 99)) == (2, 2)
    assert max(multiplexed) == 200


@pytest.mark.unit
def test_parallel_processing_still_delivers_behind_the_slowest_predecessor() -> None:
    in_order = completion_times(SLOW_FIRST, Schedule.PARALLEL_IN_ORDER)
    assert set(in_order) == {200}

    slow_last = (*([2] * 99), 200)
    assert completion_times(slow_last, Schedule.PARALLEL_IN_ORDER)[:99] == (2,) * 99


@pytest.mark.unit
def test_more_connections_bound_the_damage_to_one_lane() -> None:
    six = completion_times(SLOW_FIRST, Schedule.SERIAL, connections=6)
    lane_zero = six[0::6]
    other_lanes = [t for i, t in enumerate(six) if i % 6]

    assert lane_zero[:3] == (200, 202, 204)
    assert max(other_lanes) == 34
    assert (nearest_rank(six, 50), nearest_rank(six, 99)) == (20, 230)


@pytest.mark.unit
def test_h2_streams_complete_out_of_order_and_correlate_by_stream_id() -> None:
    finished = h2_exchange([b"/slow", b"/fast"], server_finish_order=[1, 0])
    assert finished == ((3, b"/fast"), (1, b"/slow"))

    in_order = h2_exchange([b"/slow", b"/fast"], server_finish_order=[0, 1])
    assert in_order == ((1, b"/slow"), (3, b"/fast"))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: completion_times([1], Schedule.SERIAL, connections=0), "connections"),
        (lambda: completion_times([-1], Schedule.SERIAL), "non-negative"),
        (lambda: nearest_rank([], 50), "need values"),
        (lambda: nearest_rank([1], 0), "need values"),
        (lambda: h2_exchange([b"/a", b"/b"], [0, 0]), "permutation"),
        (lambda: serve_pipelined_h11(b"HTTP/1.1 200 OK\r\n\r\n", lambda r: b""), None),
    ],
    ids=["zero-connections", "negative-service", "empty", "p0", "not-permutation", "garbage"],
)
def test_inputs_are_validated(call: object, match: str | None) -> None:
    assert callable(call)
    if match is None:
        with pytest.raises(h11.RemoteProtocolError):
            call()
    else:
        with pytest.raises(ValueError, match=match):
            call()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_app_itself_has_no_head_of_line_blocking() -> None:
    fast_done = asyncio.Event()
    completed: list[str] = []

    async def slow(request: Request) -> PlainTextResponse:
        await asyncio.wait_for(fast_done.wait(), timeout=2)
        completed.append("/slow")
        return PlainTextResponse("/slow")

    async def fast(request: Request) -> PlainTextResponse:
        completed.append("/fast")
        fast_done.set()
        return PlainTextResponse("/fast")

    app = Starlette(routes=[Route("/slow", slow), Route("/fast", fast)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow_resp, fast_resp = await asyncio.gather(client.get("/slow"), client.get("/fast"))

    assert (slow_resp.text, fast_resp.text) == ("/slow", "/fast")
    assert completed == ["/fast", "/slow"]


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("http_impl", ["h11", "httptools"])
async def test_uvicorn_holds_pipelined_request_behind_slow_one(http_impl: str) -> None:
    started: list[str] = []
    slow_started = asyncio.Event()
    release = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "http"
        path: str = scope["path"]
        started.append(path)
        if path == "/slow":
            slow_started.set()
            await release.wait()
        body = path.encode("ascii")
        headers = [(b"content-length", str(len(body)).encode("ascii"))]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    config = uvicorn.Config(app, http=http_impl, lifespan="off", log_level="warning")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Three requests, so two are queued behind /slow and a LIFO queue would answer /fast first.
        writer.write(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /mid HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(slow_started.wait(), timeout=5)

        # Bounded negative check: /mid and /fast are on the wire but must not reach the app or
        # the client.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.read(1), timeout=0.2)
        assert started == ["/slow"]
        # Back-pressure: Uvicorn stops reading the socket while the queued requests wait, so the
        # backlog stays in the kernel buffer and TCP flow control reaches the client.
        (protocol,) = server.server_state.connections
        assert isinstance(protocol.transport, asyncio.Transport)
        assert not protocol.transport.is_reading()

        release.set()
        raw = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        await writer.wait_closed()
    finally:
        release.set()  # let a failed run drain instead of masking the assertion
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        sock.close()

    assert started == ["/slow", "/mid", "/fast"]
    assert raw.count(b"HTTP/1.1 200 OK\r\n") == 3
    assert raw.index(b"\r\n\r\n/slow") < raw.index(b"\r\n\r\n/mid") < raw.index(b"\r\n\r\n/fast")
