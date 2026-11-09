"""Outbound connection pooling in httpx 0.28.1 / httpcore 1.0.9, without a network.

The lab plugs an in-memory `httpcore.AsyncNetworkBackend` under a real
`httpx.AsyncHTTPTransport`, so every pooling decision (reuse, queueing,
`PoolTimeout`, idle expiry, surplus-idle eviction) is made by the pinned
httpcore code. Only the socket and the DNS resolver are simulated:

* `FakeDNS` is an authoritative zone plus a TTL-honoring stub cache driven by a
  manual clock. httpcore resolves nothing itself; it hands the host name to
  the backend's `connect_tcp` once per new connection.
* `FakeOriginStream` is an HTTP/1.1 origin built on an `h11` server state
  machine. It answers every request with the address the connection was
  opened to, can hold responses for a path until a test releases them, and
  can send `Connection: close` after N requests.

`MaxLifetimeTransport` is the client-side fix the pool lacks: httpcore bounds
how long a connection may sit idle (`keepalive_expiry`) but not how old it may
get, so a busy connection pins its original address forever.
"""

from __future__ import annotations

import collections
import typing
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio
import h11
import httpcore
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if typing.TYPE_CHECKING:
    import ssl
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable

    from starlette.requests import Request

UPSTREAM = "http://api.internal"


class ManualClock:
    """A clock that moves only when the test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDNS:
    """An authoritative zone behind a stub cache that honors the record TTL."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._zone: dict[str, tuple[str, float]] = {}
        self._cache: dict[str, tuple[str, float]] = {}
        self.upstream_queries = 0

    def publish(self, host: str, address: str, ttl: float) -> None:
        """Change the authoritative answer; cached copies stay until their TTL ends."""
        self._zone[host] = (address, ttl)

    def resolve(self, host: str) -> str:
        cached = self._cache.get(host)
        if cached is not None and self._clock() < cached[1]:
            return cached[0]
        self.upstream_queries += 1
        address, ttl = self._zone[host]
        self._cache[host] = (address, self._clock() + ttl)
        return address


@dataclass
class _PendingResponse:
    data: bytearray
    gate: anyio.Event | None


class FakeOriginStream(httpcore.AsyncNetworkStream):
    """One TCP connection to an HTTP/1.1 origin at `address`, parsed by h11."""

    def __init__(self, backend: RecordingBackend, address: str) -> None:
        self._backend = backend
        self.address = address
        self._server = h11.Connection(our_role=h11.SERVER)
        self._pending: collections.deque[_PendingResponse] = collections.deque()
        self._target = b""
        self.requests_served = 0
        self.closed = False

    # `timeout` is part of the httpcore.AsyncNetworkStream interface; the lab ignores it.
    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        if not buffer:
            # httpcore writes b"" for the empty end of a bodiless request;
            # h11 would read receive_data(b"") as the peer half-closing.
            return
        self._server.receive_data(buffer)
        while True:
            event = self._server.next_event()
            if event is h11.NEED_DATA or event is h11.PAUSED:
                return
            if isinstance(event, h11.Request):
                self._target = event.target
            elif isinstance(event, h11.EndOfMessage):
                self._respond()

    def _respond(self) -> None:
        self.requests_served += 1
        body = self.address.encode("ascii")
        headers = [(b"content-length", str(len(body)).encode("ascii"))]
        limit = self._backend.max_requests_per_connection
        if limit is not None and self.requests_served >= limit:
            headers.append((b"connection", b"close"))
        data = bytearray()
        for out in (
            h11.Response(status_code=200, headers=headers),
            h11.Data(data=body),
            h11.EndOfMessage(),
        ):
            data += self._server.send(out) or b""
        if self._server.our_state is h11.DONE and self._server.their_state is h11.DONE:
            self._server.start_next_cycle()
        self._pending.append(_PendingResponse(data, self._backend.held.get(self._target)))
        self._backend.note_request()

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        if not self._pending:
            return b""
        head = self._pending[0]
        if head.gate is not None:
            await head.gate.wait()
        chunk = bytes(head.data[:max_bytes])
        del head.data[:max_bytes]
        if not head.data:
            self._pending.popleft()
        return chunk

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            self._backend.note_close()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        raise NotImplementedError("the lab speaks plain http://")

    def get_extra_info(self, info: str) -> object:
        # httpcore's has_expired() asks this of IDLE connections; this origin
        # never closes an idle connection on its own.
        return False if info == "is_readable" else None


@dataclass
class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Network backend that resolves through FakeDNS and records every connect."""

    dns: FakeDNS
    max_requests_per_connection: int | None = None
    held: dict[bytes, anyio.Event] = field(default_factory=dict[bytes, anyio.Event])
    streams: list[FakeOriginStream] = field(default_factory=list[FakeOriginStream])
    open_now: int = 0
    peak_open: int = 0
    requests_seen: int = 0
    _waiters: list[tuple[int, anyio.Event]] = field(
        default_factory=list[tuple[int, anyio.Event]], repr=False
    )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = FakeOriginStream(self, self.dns.resolve(host))
        self.streams.append(stream)
        self.open_now += 1
        self.peak_open = max(self.peak_open, self.open_now)
        return stream

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)

    def hold(self, path: str) -> anyio.Event:
        """Hold responses for `path` until the returned event is set."""
        gate = anyio.Event()
        self.held[path.encode("ascii")] = gate
        return gate

    def note_request(self) -> None:
        self.requests_seen += 1
        for count, event in self._waiters:
            if self.requests_seen >= count:
                event.set()

    def note_close(self) -> None:
        self.open_now -= 1

    @property
    def connects(self) -> int:
        return len(self.streams)

    def open_to(self, address: str) -> int:
        return sum(1 for s in self.streams if s.address == address and not s.closed)

    async def wait_for_requests(self, count: int) -> None:
        """Return once the origin has parsed `count` complete requests in total."""
        if self.requests_seen >= count:
            return
        event = anyio.Event()
        self._waiters.append((count, event))
        await event.wait()


class BackendTransport(httpx.AsyncHTTPTransport):
    """A stock httpx transport whose httpcore pool uses an injected backend.

    httpx 0.28.1 builds `httpcore.AsyncConnectionPool` from `limits` in
    `AsyncHTTPTransport.__init__` but exposes no `network_backend` argument,
    so the pool is rebuilt with the same limits and the lab backend.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend, limits: httpx.Limits) -> None:
        super().__init__(limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=backend,
        )


def make_client(
    backend: httpcore.AsyncNetworkBackend,
    limits: httpx.Limits,
    *,
    pool_timeout: float = 5.0,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=BackendTransport(backend, limits),
        base_url=UPSTREAM,
        timeout=httpx.Timeout(5.0, pool=pool_timeout),
    )


@dataclass
class _Generation:
    transport: httpx.AsyncBaseTransport
    born: float
    in_flight: int = 0
    retired: bool = False


class _DrainingStream(httpx.AsyncByteStream):
    def __init__(
        self, inner: httpx.AsyncByteStream, on_close: Callable[[], typing.Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._on_close = on_close
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._inner.aclose()
            await self._on_close()


class MaxLifetimeTransport(httpx.AsyncBaseTransport):
    """Retire the inner transport after `max_lifetime` seconds so new connections re-resolve.

    The retired transport keeps serving responses already in flight and is
    closed when the last of them is closed, so rotation never cuts a request.
    """

    def __init__(
        self,
        factory: Callable[[], httpx.AsyncBaseTransport],
        max_lifetime: float,
        clock: Callable[[], float],
    ) -> None:
        self._factory = factory
        self._max_lifetime = max_lifetime
        self._clock = clock
        self._current = _Generation(factory(), clock())
        self._retired: list[_Generation] = []

    def _generation(self) -> _Generation:
        if self._clock() - self._current.born >= self._max_lifetime:
            self._current.retired = True
            self._retired.append(self._current)
            self._current = _Generation(self._factory(), self._clock())
        return self._current

    async def _release(self, gen: _Generation) -> None:
        gen.in_flight -= 1
        if gen.retired and gen.in_flight == 0:
            self._retired.remove(gen)
            await gen.transport.aclose()

    async def _reap_idle_retired(self) -> None:
        for gen in [g for g in self._retired if g.in_flight == 0]:
            self._retired.remove(gen)
            await gen.transport.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gen = self._generation()
        await self._reap_idle_retired()
        gen.in_flight += 1
        try:
            response = await gen.transport.handle_async_request(request)
        except BaseException:
            await self._release(gen)
            raise
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await self._release(gen)
            raise TypeError("an async transport must return an async byte stream")

        async def release() -> None:
            await self._release(gen)

        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_DrainingStream(response.stream, release),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        for gen in [*self._retired, self._current]:
            await gen.transport.aclose()
        self._retired.clear()


@dataclass
class _GatewayState:
    client: httpx.AsyncClient | None = None


def build_gateway(
    backend: httpcore.AsyncNetworkBackend, limits: httpx.Limits, *, shared: bool
) -> Starlette:
    """An inbound API whose `/proxy` handler calls the upstream once per request.

    `shared=True` creates one client in the lifespan and closes it at shutdown;
    `shared=False` is the per-request anti-pattern, one client (and pool) per call.
    """
    state = _GatewayState()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        if not shared:
            yield
            return
        async with make_client(backend, limits) as client:
            state.client = client
            yield
        state.client = None

    async def proxy(request: Request) -> JSONResponse:
        if state.client is not None:
            upstream = await state.client.get("/v1/price")
        else:
            async with make_client(backend, limits) as client:
                upstream = await client.get("/v1/price")
        return JSONResponse({"served_by": upstream.text})

    return Starlette(routes=[Route("/proxy", proxy)], lifespan=lifespan)
