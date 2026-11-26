"""Lab for Propagating-Client-Disconnects-to-Cancel-Upstream-GPU-Inference."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import httpcore
import pytest
import uvicorn

from api_labs.f21_ai_and_llm_api_engineering.abandoned_request_cancellation import (
    ASGIApp,
    ASGIConnection,
    ConnectionClosed,
    FakeGPUEngine,
    GatewayLog,
    buffering_gateway,
    make_upstream_client,
    read_request_body,
    serve_stream,
    starlette_gateway,
    watching_gateway,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

HANG_GUARD_SECONDS = 2.0


async def settle(turns: int = 20) -> None:
    """Let every ready task run; a negative claim is only meaningful after this."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), HANG_GUARD_SECONDS)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watching_gateway_aborts_engine_and_closes_httpx_after_three_tokens() -> None:
    engine = FakeGPUEngine()
    log = GatewayLog()
    conn = ASGIConnection()
    app = watching_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    engine.release(3)
    await asyncio.wait_for(conn.wait_for_body_chunks(3), HANG_GUARD_SECONDS)
    assert engine.steps == 3
    assert engine.slots_in_use == 1

    conn.disconnect()
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)

    assert engine.aborted
    assert not engine.finished
    assert engine.steps == 3
    assert engine.slots_in_use == 0
    assert engine.upstream_closed
    assert log.outcomes == ["client_disconnect"]
    assert conn.body_text().count("data: ") == 3
    assert conn.disconnected_at is not None
    assert engine.aborted_at is not None
    assert engine.upstream_closed_at is not None
    abort_ms = (engine.aborted_at - conn.disconnected_at) * 1000
    close_ms = (engine.upstream_closed_at - conn.disconnected_at) * 1000
    print(f"in-process abort latency {abort_ms:.3f} ms, httpx close latency {close_ms:.3f} ms")
    assert 0 <= abort_ms <= close_ms < HANG_GUARD_SECONDS * 1000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buffering_gateway_keeps_decoding_after_disconnect() -> None:
    engine = FakeGPUEngine(max_tokens=20, free_run=True)
    log = GatewayLog()
    conn = ASGIConnection()
    app = buffering_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    await asyncio.wait_for(conn.wait_for_body_chunks(3), HANG_GUARD_SECONDS)
    conn.disconnect()
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)

    assert engine.finished
    assert not engine.aborted
    assert engine.steps == 20
    assert conn.body_chunks < 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_streaming_response_cancels_upstream_on_spec_2_3() -> None:
    engine = FakeGPUEngine()
    log = GatewayLog()
    conn = ASGIConnection(spec_version="2.3")
    app = starlette_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    engine.release(3)
    await asyncio.wait_for(conn.wait_for_body_chunks(3), HANG_GUARD_SECONDS)
    conn.disconnect()
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)
    await wait_for(engine.upstream_closed_event)

    assert engine.aborted
    assert engine.steps == 3
    assert engine.slots_in_use == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_spec_2_4_misses_a_disconnect_while_upstream_is_silent() -> None:
    engine = FakeGPUEngine()
    log = GatewayLog()
    conn = ASGIConnection(spec_version="2.4")
    app = starlette_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    engine.release(3)
    await asyncio.wait_for(conn.wait_for_body_chunks(3), HANG_GUARD_SECONDS)
    conn.disconnect()
    await settle()

    # Nothing is sent while the engine is silent, so no send() can raise.
    assert not task.done()
    assert not engine.aborted
    assert engine.slots_in_use == 1

    # The next token is the first send() on the closed connection: one wasted decode step.
    engine.release(1)
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)
    await wait_for(engine.upstream_closed_event)
    assert engine.steps == 4
    assert engine.aborted
    assert log.outcomes == ["client_disconnect"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watching_gateway_aborts_on_spec_2_4_even_while_upstream_is_silent() -> None:
    engine = FakeGPUEngine()
    log = GatewayLog()
    conn = ASGIConnection(spec_version="2.4")
    app = watching_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    engine.release(3)
    await asyncio.wait_for(conn.wait_for_body_chunks(3), HANG_GUARD_SECONDS)
    conn.disconnect()
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)

    assert engine.steps == 3
    assert engine.aborted
    assert engine.upstream_closed
    assert log.outcomes == ["client_disconnect"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_during_prefill_aborts_before_any_token_exists() -> None:
    engine = FakeGPUEngine()
    engine.prefill_gate.clear()
    log = GatewayLog()
    conn = ASGIConnection()
    app = watching_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    await wait_for(engine.admitted)
    assert engine.slots_in_use == 1
    conn.disconnect()
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)

    assert engine.steps == 0
    assert engine.aborted
    assert engine.slots_in_use == 0
    assert conn.body_chunks == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_before_request_body_completes_never_touches_the_engine() -> None:
    engine = FakeGPUEngine()
    log = GatewayLog()
    conn = ASGIConnection(body_parts=(b'{"prompt": ',), body_complete=False)
    app = watching_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    await settle()
    assert not task.done()
    conn.disconnect()
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)

    assert engine.admissions == 0
    assert conn.sent == []
    assert log.outcomes == ["disconnected_before_upstream"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_normal_completion_is_not_reported_or_aborted_as_a_disconnect() -> None:
    engine = FakeGPUEngine(max_tokens=3)
    log = GatewayLog()
    conn = ASGIConnection()
    app = watching_gateway(make_upstream_client(engine), log)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    engine.release(3)
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)

    assert engine.finished
    assert not engine.aborted
    assert engine.slots_in_use == 0
    assert engine.upstream_closed
    assert log.outcomes == ["completed"]
    assert conn.sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert conn.body_chunks == 3


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("hold_sends", [True, False], ids=["send-blocked", "upstream-silent"])
@pytest.mark.parametrize(
    ("shield_close", "expect_closed"),
    [(True, True), (False, False)],
    ids=["shielded", "unshielded"],
)
async def test_upstream_release_under_cancellation_needs_a_shield(
    hold_sends: bool, shield_close: bool, expect_closed: bool
) -> None:
    engine = FakeGPUEngine()
    log = GatewayLog()
    conn = ASGIConnection()
    client = make_upstream_client(engine, close_checkpoints=True)
    app = watching_gateway(client, log, shield_close=shield_close)
    task = asyncio.create_task(app(conn.scope, conn.receive, conn.send))

    if hold_sends:
        await wait_for(engine.admitted)
        conn.hold_sends()
        engine.release(1)
        await wait_for(conn.send_blocked)
        assert engine.steps == 1
    else:
        engine.release(1)
        await asyncio.wait_for(conn.wait_for_body_chunks(1), HANG_GUARD_SECONDS)
    conn.disconnect()
    await asyncio.wait_for(task, HANG_GUARD_SECONDS)
    await settle()

    leaked = hold_sends and not shield_close
    assert engine.upstream_closed is expect_closed
    assert engine.aborted is (not leaked)
    assert engine.slots_in_use == (1 if leaked else 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connection_driver_raises_on_spec_2_4_and_drops_on_spec_2_3() -> None:
    message = {"type": "http.response.body", "body": b"x", "more_body": True}
    modern = ASGIConnection(spec_version="2.4")
    legacy = ASGIConnection(spec_version="2.3")
    modern.disconnect()
    legacy.disconnect()
    with pytest.raises(ConnectionClosed):
        await modern.send(message)
    await legacy.send(message)
    assert legacy.dropped == 1
    assert legacy.sent == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_request_body_joins_parts_and_reports_early_disconnect() -> None:
    whole = ASGIConnection(body_parts=(b"ab", b"cd"))
    assert await read_request_body(whole.receive) == b"abcd"
    partial = ASGIConnection(body_parts=(b"ab",), body_complete=False)
    partial.disconnect()
    assert await read_request_body(partial.receive) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upstream_oserror_is_not_reported_as_a_client_disconnect() -> None:
    conn = ASGIConnection()

    async def tokens() -> AsyncGenerator[bytes, None]:
        yield b"data: first\n\n"
        raise OSError("upstream read failed")

    with pytest.RaisesGroup(pytest.RaisesExc(OSError, match="upstream read failed")):
        await serve_stream(conn.receive, conn.send, tokens())
    assert conn.body_chunks == 1


class H2Peer(httpcore.AsyncNetworkStream):
    """An h2 server that leaves its first response open until explicitly finished."""

    def __init__(self) -> None:
        self.protocol = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        self.protocol.initiate_connection()
        self.request_ids: list[int] = []
        self.reset_ids: list[int] = []
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        outgoing = self.protocol.data_to_send(max_bytes)
        assert outgoing, "httpcore read when the peer had no pending frames"
        return outgoing

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        for event in self.protocol.receive_data(buffer):
            if isinstance(event, h2.events.RequestReceived):
                stream_id = event.stream_id
                self.request_ids.append(stream_id)
                self.protocol.send_headers(stream_id, [(b":status", b"200")])
                if len(self.request_ids) == 1:
                    self.protocol.send_data(stream_id, b"first")
                else:
                    self.protocol.send_data(stream_id, b"sibling", end_stream=True)
            elif isinstance(event, h2.events.StreamReset):
                self.reset_ids.append(event.stream_id)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpcore_http11_early_close_closes_the_network_stream() -> None:
    peer = httpcore.AsyncMockStream(
        [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nfirst\r\n"]
    )
    conn = httpcore.AsyncHTTP11Connection(httpcore.Origin(b"http", b"engine.test", 80), peer)
    async with contextlib.aclosing(conn):
        async with conn.stream("GET", "http://engine.test/stream") as response:
            assert await anext(response.aiter_stream()) == b"first"
        assert conn.is_closed()
        with pytest.raises(httpcore.ReadError, match="Connection closed"):
            await peer.read(1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpcore_http2_early_close_leaves_the_peer_stream_open() -> None:
    peer = H2Peer()
    conn = httpcore.AsyncHTTP2Connection(httpcore.Origin(b"http", b"engine.test", 80), peer)
    async with contextlib.aclosing(conn):
        async with conn.stream("GET", "http://engine.test/stream") as response:
            assert await anext(response.aiter_stream()) == b"first"
        assert not peer.closed
        assert peer.reset_ids == []
        assert peer.request_ids == [1]

        # The peer can still send on stream 1, while httpcore serves stream 3.
        peer.protocol.send_data(1, b"after close", end_stream=True)
        assert (await conn.request("GET", "http://engine.test/sibling")).content == b"sibling"
        assert peer.request_ids == [1, 3]
        assert peer.reset_ids == []


@contextlib.asynccontextmanager
async def running_uvicorn(app: ASGIApp, http: str) -> AsyncGenerator[int, None]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="off",
        http=http,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 - uvicorn exposes no startup event
            await asyncio.sleep(0.005)
        sockets = server.servers[0].sockets
        yield sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


async def read_until_events(reader: asyncio.StreamReader, count: int) -> bytes:
    buffer = b""
    while buffer.count(b"data: ") < count:
        chunk = await asyncio.wait_for(reader.read(4096), HANG_GUARD_SECONDS)
        assert chunk, "server closed the stream early"
        buffer += chunk
    return buffer


REQUEST = (
    b"POST /v1/stream HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
    b"Content-Length: 16\r\n\r\n" + b'{"prompt": "hi"}'
)


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("http", ["h11", "httptools"])
@pytest.mark.parametrize("how", ["close", "half-close"])
async def test_real_uvicorn_socket_close_aborts_the_engine_after_three_events(
    http: str, how: str
) -> None:
    engine = FakeGPUEngine()
    log = GatewayLog()
    app = watching_gateway(make_upstream_client(engine), log)
    async with running_uvicorn(app, http) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(REQUEST)
        engine.release(3)
        await read_until_events(reader, 3)
        started = time.perf_counter()
        if how == "close":
            writer.close()
        else:
            writer.write_eof()
        await wait_for(engine.done)
        elapsed_ms = (time.perf_counter() - started) * 1000
        writer.close()
    print(f"uvicorn {uvicorn.__version__} {http} {how}: engine abort after {elapsed_ms:.2f} ms")

    assert engine.aborted
    assert engine.steps == 3
    assert engine.slots_in_use == 0
    assert engine.upstream_closed
    assert log.outcomes == ["client_disconnect"]
    assert elapsed_ms < HANG_GUARD_SECONDS * 1000
