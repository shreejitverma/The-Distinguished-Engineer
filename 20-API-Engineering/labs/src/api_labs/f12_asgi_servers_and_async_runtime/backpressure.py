"""End-to-end backpressure: slow TCP reader -> transport watermarks -> uvicorn send().

Two harnesses drive the real uvicorn 0.54.0 HTTP/1.1 protocol classes and a real Starlette 1.10.0
`StreamingResponse`:

- `start_uvicorn` connects an `H11Protocol` or `HttpToolsProtocol` to `SlowClientTransport`, an
  in-memory transport that models the kernel (send buffer plus the peer's receive window) as a fixed
  byte capacity and applies the CPython asyncio watermark rules to everything beyond it.
  It is deterministic and opens no sockets.
- `measure_loopback_stall` serves the same app on `127.0.0.1:0` through a real asyncio (or uvloop)
  transport and a real kernel TCP stack, with a client that stops reading.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route
from uvicorn.config import Config
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
from uvicorn.server import ServerState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

# CPython Lib/asyncio/transports.py, _FlowControlMixin._set_write_buffer_limits defaults.
ASYNCIO_DEFAULT_HIGH = 64 * 1024
ASYNCIO_DEFAULT_LOW = ASYNCIO_DEFAULT_HIGH // 4
REQUEST = b"GET /stream HTTP/1.1\r\nHost: test\r\n\r\n"
CHUNKED_TERMINATOR = b"0\r\n\r\n"

HttpImpl = Literal["h11", "httptools"]
UvicornProtocol = H11Protocol | HttpToolsProtocol


def chunk_frame_size(chunk_size: int) -> int:
    """Wire bytes of one body chunk in HTTP/1.1 chunked coding (RFC 9112 Section 7.1)."""
    return len(f"{chunk_size:x}\r\n") + chunk_size + len(b"\r\n")


class SlowClientTransport(asyncio.Transport):
    """In-memory TCP stand-in whose peer reads only when the test calls `client_read`.

    The first `kernel_capacity` bytes are accepted at once, as `socket.send` would accept them into
    the kernel send buffer and the peer's receive window.
    Everything after that waits in a user-space buffer, and the protocol is paused and resumed with
    the same strict comparisons as CPython `_FlowControlMixin`: pause when the buffer is strictly
    greater than `high`, resume when it is less than or equal to `low`.
    """

    def __init__(
        self,
        protocol: asyncio.BaseProtocol,
        *,
        kernel_capacity: int,
        high: int = ASYNCIO_DEFAULT_HIGH,
        low: int | None = None,
    ) -> None:
        super().__init__(
            extra={"peername": ("127.0.0.1", 50_000), "sockname": ("127.0.0.1", 8_000)}
        )
        self._protocol = protocol
        self.kernel_capacity = kernel_capacity
        self.high = high
        self.low = high // 4 if low is None else low
        self._kernel = bytearray()
        self._buffer = bytearray()
        self.delivered = bytearray()
        self.total_written = 0
        self.max_buffered = 0
        self.pauses = 0
        self.resumes = 0
        self.protocol_paused = False
        self.reading_paused = False
        self._closing = False

    # asyncio.WriteTransport
    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._closing or not data:
            return
        payload = bytes(data)
        self.total_written += len(payload)
        if not self._buffer:
            room = max(self.kernel_capacity - len(self._kernel), 0)
            self._kernel += payload[:room]
            payload = payload[room:]
        self._buffer += payload
        self.max_buffered = max(self.max_buffered, len(self._buffer))
        if len(self._buffer) > self.high and not self.protocol_paused:
            self.protocol_paused = True
            self.pauses += 1
            self._protocol.pause_writing()

    def get_write_buffer_size(self) -> int:
        return len(self._buffer)

    def get_write_buffer_limits(self) -> tuple[int, int]:
        return (self.low, self.high)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            asyncio.get_running_loop().call_soon(self._protocol.connection_lost, None)

    def abort(self) -> None:
        self.disconnect(ConnectionAbortedError("transport aborted"))

    def can_write_eof(self) -> bool:
        return False

    # asyncio.ReadTransport
    def pause_reading(self) -> None:
        self.reading_paused = True

    def resume_reading(self) -> None:
        self.reading_paused = False

    def is_reading(self) -> bool:
        return not self.reading_paused

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        self._protocol = protocol

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol

    # The slow client
    @property
    def in_kernel(self) -> int:
        return len(self._kernel)

    def client_read(self, nbytes: int) -> int:
        """The peer reads up to *nbytes*; the freed window is refilled from the user buffer."""
        taken = bytes(self._kernel[:nbytes])
        del self._kernel[:nbytes]
        self.delivered += taken
        room = self.kernel_capacity - len(self._kernel)
        if room > 0 and self._buffer:
            self._kernel += self._buffer[:room]
            del self._buffer[:room]
        if self.protocol_paused and len(self._buffer) <= self.low:
            self.protocol_paused = False
            self.resumes += 1
            self._protocol.resume_writing()
        return len(taken)

    def disconnect(self, exc: Exception | None) -> None:
        """The peer vanishes: a reset (or EOF when *exc* is None) reaches the protocol."""
        if self._closing:
            return
        self._closing = True
        self._protocol.connection_lost(exc)


@dataclass
class StreamProbe:
    """What the response producer did: chunks it generated and whether its cleanup ran."""

    chunk_size: int
    produced_chunks: int = 0
    closed: bool = False

    @property
    def produced_bytes(self) -> int:
        return self.produced_chunks * self.chunk_size


async def counting_body(probe: StreamProbe, total_chunks: int) -> AsyncIterator[bytes]:
    """An async generator that never awaits: it produces as fast as send() lets it."""
    chunk = b"x" * probe.chunk_size
    try:
        for _ in range(total_chunks):
            probe.produced_chunks += 1
            yield chunk
    finally:
        probe.closed = True


async def awaiting_body(probe: StreamProbe, total_chunks: int) -> AsyncIterator[bytes]:
    """The same generator with one real suspension per chunk, like awaiting a database row."""
    chunk = b"x" * probe.chunk_size
    try:
        for _ in range(total_chunks):
            probe.produced_chunks += 1
            yield chunk
            await asyncio.sleep(0)
    finally:
        probe.closed = True


def streaming_app(make_body: Callable[[], AsyncIterator[bytes]]) -> Starlette:
    """A Starlette app whose `GET /stream` returns `StreamingResponse(make_body())`."""

    async def stream(request: Request) -> StreamingResponse:
        return StreamingResponse(make_body(), media_type="application/octet-stream")

    return Starlette(routes=[Route("/stream", stream)])


def send_loop_app(probe: StreamProbe, total_chunks: int) -> ASGIApp:
    """A raw ASGI app that awaits send() per chunk but never calls receive()."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        chunk = b"x" * probe.chunk_size
        headers = [(b"content-type", b"application/octet-stream")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for _ in range(total_chunks):
            probe.produced_chunks += 1
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        probe.closed = True

    return app


def single_send_app(body: bytes) -> ASGIApp:
    """A raw ASGI app that hands the whole body to one send() call."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"content-length", str(len(body)).encode("ascii"))]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


@dataclass
class QueueRelay:
    """Producer task -> asyncio.Queue -> send loop; `maxsize=0` is the unbounded anti-pattern."""

    probe: StreamProbe
    total_chunks: int
    maxsize: int
    queue: asyncio.Queue[bytes | None] = field(init=False)
    peak_queued_bytes: int = 0

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.maxsize)

    async def _produce(self) -> None:
        chunk = b"x" * self.probe.chunk_size
        for _ in range(self.total_chunks):
            await self.queue.put(chunk)
            self.probe.produced_chunks += 1
            self.peak_queued_bytes = max(
                self.peak_queued_bytes, self.queue.qsize() * self.probe.chunk_size
            )
            await asyncio.sleep(0)  # stands in for awaiting a database or upstream read
        await self.queue.put(None)

    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"content-type", b"application/octet-stream")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        async with asyncio.TaskGroup() as group:
            group.create_task(self._produce())
            while (chunk := await self.queue.get()) is not None:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        self.probe.closed = True


@dataclass
class UvicornHarness:
    """One uvicorn HTTP/1.1 connection wired to a `SlowClientTransport`."""

    protocol: UvicornProtocol
    transport: SlowClientTransport
    server_state: ServerState

    @property
    def write_paused(self) -> bool:
        return self.protocol.flow.write_paused

    @property
    def app_tasks(self) -> int:
        return len(self.server_state.tasks)


def uvicorn_config(app: ASGIApp, http: HttpImpl) -> Config:
    # interface="asgi3" because uvicorn's auto-detection treats a bound method as ASGI2.
    config = Config(
        app=app, http=http, interface="asgi3", lifespan="off", log_config=None, access_log=False
    )
    config.load()
    return config


def start_uvicorn(
    app: ASGIApp, *, http: HttpImpl, kernel_capacity: int, high: int = ASYNCIO_DEFAULT_HIGH
) -> UvicornHarness:
    """Accept one connection on the in-memory transport and feed it `GET /stream`."""
    config = uvicorn_config(app, http)
    state = ServerState()
    loop = asyncio.get_running_loop()
    protocol_class = H11Protocol if http == "h11" else HttpToolsProtocol
    protocol = protocol_class(config=config, server_state=state, app_state={}, _loop=loop)
    transport = SlowClientTransport(protocol, kernel_capacity=kernel_capacity, high=high)
    protocol.connection_made(transport)
    protocol.data_received(REQUEST)
    return UvicornHarness(protocol=protocol, transport=transport, server_state=state)


async def run_until_quiet(
    observe: Callable[[], object], *, quiet_steps: int = 50, limit: int = 1_000_000
) -> int:
    """Yield to the loop until `observe()` is unchanged for `quiet_steps` iterations.

    No I/O happens in the in-memory harness, so this is deterministic: it returns the number of
    loop iterations it ran.
    """
    last = observe()
    steady = 0
    for step in range(limit):
        await asyncio.sleep(0)
        current = observe()
        steady = steady + 1 if current == last else 0
        last = current
        if steady >= quiet_steps:
            return step
    raise RuntimeError("producer never went quiet")


@dataclass(frozen=True)
class TickSample:
    tick: int
    produced_bytes: int
    delivered_bytes: int
    user_buffer: int
    in_kernel: int
    paused: bool


async def simulate_slow_client(
    harness: UvicornHarness, probe: StreamProbe, *, read_per_tick: int, ticks: int
) -> list[TickSample]:
    """Each tick the client reads `read_per_tick` bytes (1 KiB per tick models 1 KB/s)."""
    samples: list[TickSample] = []
    transport = harness.transport
    for tick in range(1, ticks + 1):
        transport.client_read(read_per_tick)
        await run_until_quiet(lambda: (probe.produced_chunks, transport.total_written))
        samples.append(
            TickSample(
                tick=tick,
                produced_bytes=probe.produced_bytes,
                delivered_bytes=len(transport.delivered),
                user_buffer=transport.get_write_buffer_size(),
                in_kernel=transport.in_kernel,
                paused=harness.write_paused,
            )
        )
    return samples


async def hang_up(harness: UvicornHarness, probe: StreamProbe) -> None:
    """Reset the connection and let the app finish, so no task outlives the test."""
    harness.transport.disconnect(ConnectionResetError("peer reset"))
    await run_until_quiet(lambda: (probe.produced_chunks, probe.closed, harness.app_tasks))


# Real sockets


@dataclass(frozen=True)
class LoopbackStall:
    """What a real kernel and event loop absorbed before uvicorn suspended send()."""

    loop_name: str
    write_buffer_limits: tuple[int, int]
    server_sndbuf: int
    client_rcvbuf: int
    produced_at_stall: int
    user_buffer_at_stall: int
    write_paused_at_stall: bool
    total_bytes: int
    produced_at_end: int
    generator_closed: bool
    received_bytes: int
    saw_terminator: bool

    @property
    def beyond_user_buffer_at_stall(self) -> int:
        """Body bytes produced but not in the transport buffer: in the kernel or already read."""
        return self.produced_at_stall - self.user_buffer_at_stall


async def _wait_quiet(observe: Callable[[], int], *, quiet_s: float, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last = observe()
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.01)
        current = observe()
        now = time.monotonic()
        if current != last:
            last, last_change = current, now
        elif now - last_change >= quiet_s:
            return
    raise TimeoutError("producer never stalled")


async def loopback_write_buffer_limits() -> tuple[int, int]:
    """`get_write_buffer_limits()` of a freshly accepted TCP transport on the running loop."""
    loop = asyncio.get_running_loop()
    accepted: asyncio.Future[asyncio.BaseTransport] = loop.create_future()

    class Capture(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            accepted.set_result(transport)

    server = await loop.create_server(Capture, host="127.0.0.1", port=0)
    try:
        port: int = server.sockets[0].getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.setblocking(False)
            await loop.sock_connect(client, ("127.0.0.1", port))
            transport = await asyncio.wait_for(accepted, 5.0)
            # uvloop's TCPTransport does not subclass asyncio.WriteTransport, so cast.
            limits = cast("asyncio.WriteTransport", transport).get_write_buffer_limits()
            transport.close()
    finally:
        server.close()
        await server.wait_closed()
    return limits


async def measure_loopback_stall(
    *,
    chunk_size: int = 16_384,
    total_chunks: int = 4_096,
    client_rcvbuf: int = 65_536,
    quiet_s: float = 0.3,
    timeout_s: float = 20.0,
) -> LoopbackStall:
    """Serve an endless-looking StreamingResponse to a client that stops reading, then drains."""
    loop = asyncio.get_running_loop()
    probe = StreamProbe(chunk_size=chunk_size)
    config = uvicorn_config(streaming_app(lambda: counting_body(probe, total_chunks)), "h11")
    state = ServerState()
    protocols: list[H11Protocol] = []

    def factory() -> H11Protocol:
        protocol = H11Protocol(config=config, server_state=state, app_state={}, _loop=loop)
        protocols.append(protocol)
        return protocol

    server = await loop.create_server(factory, host="127.0.0.1", port=0)
    try:
        port: int = server.sockets[0].getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            # Before connect, so the advertised window scale reflects the small buffer.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, client_rcvbuf)
            client.setblocking(False)
            await loop.sock_connect(client, ("127.0.0.1", port))
            await loop.sock_sendall(client, REQUEST)
            await _wait_quiet(lambda: probe.produced_chunks, quiet_s=quiet_s, timeout_s=timeout_s)
            protocol = protocols[0]
            transport = protocol.transport
            server_sock: Any = transport.get_extra_info("socket")
            server_sndbuf = int(server_sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF))
            produced_at_stall = probe.produced_bytes
            user_buffer_at_stall = transport.get_write_buffer_size()
            write_paused_at_stall = protocol.flow.write_paused
            rcvbuf = int(client.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
            received = 0
            tail = b""
            while tail != CHUNKED_TERMINATOR:
                data = await asyncio.wait_for(loop.sock_recv(client, 1 << 20), timeout_s)
                if not data:
                    break
                received += len(data)
                tail = (tail + data)[-len(CHUNKED_TERMINATOR) :]
        for protocol in protocols:
            protocol.transport.close()
    finally:
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout_s)
    return LoopbackStall(
        loop_name=type(loop).__module__ + "." + type(loop).__name__,
        write_buffer_limits=transport.get_write_buffer_limits(),
        server_sndbuf=server_sndbuf,
        client_rcvbuf=rcvbuf,
        produced_at_stall=produced_at_stall,
        user_buffer_at_stall=user_buffer_at_stall,
        write_paused_at_stall=write_paused_at_stall,
        total_bytes=total_chunks * chunk_size,
        produced_at_end=probe.produced_bytes,
        generator_closed=probe.closed,
        received_bytes=received,
        saw_terminator=tail == CHUNKED_TERMINATOR,
    )
