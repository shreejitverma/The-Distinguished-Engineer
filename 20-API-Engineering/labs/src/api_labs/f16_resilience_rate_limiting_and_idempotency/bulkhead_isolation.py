"""Bulkheads for outbound calls: per-dependency pools, semaphores, tenant tiers, shuffle shards.

The downstream services are in-memory HTTP/1.1 peers behind a custom httpcore network
backend, so the real ``httpcore.AsyncConnectionPool`` queueing and ``PoolTimeout`` paths run
without opening a socket.
"""

from __future__ import annotations

import hashlib
import itertools
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import httpcore
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterable, Mapping

    from starlette.requests import Request

RECS = "recs.internal"
PAYMENTS = "payments.internal"


class Downstream:
    """One fake dependency; requests block at ``gate`` until a test opens it."""

    def __init__(self, body: bytes, *, hang: bool = False) -> None:
        self.response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        self.gate = anyio.Event()
        if not hang:
            self.gate.set()
        self.open_connections = 0
        self.peak_connections = 0
        self.requests_seen = 0
        self.waiting_at_gate = 0


class _PeerStream(httpcore.AsyncNetworkStream):
    def __init__(self, peer: Downstream) -> None:
        self._peer = peer
        self._inbox = bytearray()
        self._closed = False

    # httpcore's AsyncNetworkStream interface fixes these signatures.
    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self._inbox.extend(buffer)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        end = self._inbox.find(b"\r\n\r\n")
        if end < 0:
            return b""
        del self._inbox[: end + 4]  # bodiless requests only
        self._peer.requests_seen += 1
        self._peer.waiting_at_gate += 1
        try:
            with anyio.fail_after(timeout):
                await self._peer.gate.wait()
        except TimeoutError as exc:
            raise httpcore.ReadTimeout("peer did not answer") from exc
        finally:
            self._peer.waiting_at_gate -= 1
        return self._peer.response

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._peer.open_connections -= 1


class InMemoryNetwork(httpcore.AsyncNetworkBackend):
    """Maps a hostname to a ``Downstream``; every ``connect_tcp`` is one new connection."""

    def __init__(self, peers: Mapping[str, Downstream]) -> None:
        self.peers = dict(peers)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        peer = self.peers.get(host)
        if peer is None:
            raise httpcore.ConnectError(f"no route to {host}")
        peer.open_connections += 1
        peer.peak_connections = max(peer.peak_connections, peer.open_connections)
        return _PeerStream(peer)

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


class InMemoryPoolTransport(httpx.AsyncBaseTransport):
    """A real ``httpcore.AsyncConnectionPool`` over ``InMemoryNetwork`` (one pool per instance)."""

    def __init__(self, network: InMemoryNetwork, *, max_connections: int) -> None:
        self.pool = httpcore.AsyncConnectionPool(
            max_connections=max_connections, network_backend=network
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=await request.aread(),
            extensions=request.extensions,  # carries {"timeout": {"pool": ...}}
        )
        try:
            core_response = await self.pool.handle_async_request(core_request)
        except httpcore.PoolTimeout as exc:
            raise httpx.PoolTimeout(str(exc), request=request) from exc
        try:
            body = await core_response.aread()
        except httpcore.ReadTimeout as exc:
            raise httpx.ReadTimeout(str(exc), request=request) from exc
        finally:
            await core_response.aclose()  # returns the connection to the pool
        return httpx.Response(core_response.status, headers=core_response.headers, content=body)

    async def aclose(self) -> None:
        await self.pool.aclose()


class _UnroutedTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no mounted pool for {request.url.host}", request=request)


def shared_client(
    network: InMemoryNetwork, *, max_connections: int, pool_timeout: float
) -> httpx.AsyncClient:
    """The anti-pattern: one pool (one ``max_connections`` budget) for every dependency."""
    transport = InMemoryPoolTransport(network, max_connections=max_connections)
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0, pool=pool_timeout))


def partitioned_client(
    network: InMemoryNetwork, *, limits: Mapping[str, int], pool_timeout: float
) -> httpx.AsyncClient:
    """One client, one pool per dependency origin, routed by httpx ``mounts``."""
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {
        f"http://{host}": InMemoryPoolTransport(network, max_connections=size)
        for host, size in limits.items()
    }
    return httpx.AsyncClient(
        mounts=mounts,
        transport=_UnroutedTransport(),
        timeout=httpx.Timeout(5.0, pool=pool_timeout),
    )


class BulkheadFullError(Exception):
    """Raised instead of queueing when a bulkhead has no free slot within ``max_wait``."""

    def __init__(self, bulkhead: str) -> None:
        super().__init__(f"bulkhead {bulkhead!r} is full")
        self.bulkhead = bulkhead


class Bulkhead:
    """Semaphore bulkhead: at most ``max_concurrent`` calls in flight, bounded FIFO wait."""

    def __init__(self, name: str, max_concurrent: int, max_wait: float = 0.0) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_wait < 0:
            raise ValueError("max_wait must be >= 0")
        self.name = name
        self.max_wait = max_wait
        self._limiter = anyio.CapacityLimiter(max_concurrent)
        self.admitted = 0
        self.rejected = 0

    @property
    def in_use(self) -> int:
        return self._limiter.borrowed_tokens

    @property
    def waiting(self) -> int:
        return self._limiter.statistics().tasks_waiting

    @asynccontextmanager
    async def slot(self) -> AsyncGenerator[None]:
        # One borrower per call, not per task: a task that holds two calls holds two slots.
        borrower = object()
        await self._enter(borrower)
        try:
            yield
        finally:
            self._limiter.release_on_behalf_of(borrower)

    async def _enter(self, borrower: object) -> None:
        if self.max_wait == 0:
            try:
                self._limiter.acquire_on_behalf_of_nowait(borrower)
            except anyio.WouldBlock:
                self.rejected += 1
                raise BulkheadFullError(self.name) from None
            self.admitted += 1
            return
        with anyio.move_on_after(self.max_wait):
            await self._limiter.acquire_on_behalf_of(borrower)
            self.admitted += 1
            return
        self.rejected += 1
        raise BulkheadFullError(self.name)


@dataclass(frozen=True, slots=True)
class TierPolicy:
    tier_limit: int  # slots reserved for the whole tier; other tiers can never use them
    tenant_limit: int  # slots any single tenant of this tier may hold


class TenantBulkheads:
    """Two-level partition: a per-tenant cap nested inside a per-tier reservation."""

    def __init__(self, policies: Mapping[str, TierPolicy]) -> None:
        self._policies = dict(policies)
        self._tiers = {t: Bulkhead(f"tier:{t}", p.tier_limit) for t, p in policies.items()}
        self._tenants: dict[str, Bulkhead] = {}

    @property
    def tracked_tenants(self) -> int:
        return len(self._tenants)

    def tier(self, tier: str) -> Bulkhead:
        return self._tiers[tier]

    @asynccontextmanager
    async def slot(self, tenant: str, tier: str) -> AsyncGenerator[None]:
        policy = self._policies[tier]  # unknown tier -> KeyError: fail closed
        key = f"tenant:{tier}:{tenant}"
        head = self._tenants.get(key)
        if head is None:
            head = self._tenants[key] = Bulkhead(key, policy.tenant_limit)
        try:
            async with head.slot(), self._tiers[tier].slot():
                yield
        finally:
            if head.in_use == 0:
                del self._tenants[key]  # bounded memory under high tenant-ID cardinality


def build_storefront(
    client: httpx.AsyncClient, bulkheads: Mapping[str, Bulkhead] | None = None
) -> Starlette:
    """``/home`` degrades without recommendations; ``POST /checkout`` must reach payments."""
    heads = dict(bulkheads or {})

    async def call(host: str) -> httpx.Response:
        head = heads.get(host)
        if head is None:
            return await client.get(f"http://{host}/")
        async with head.slot():
            return await client.get(f"http://{host}/")

    async def home(request: Request) -> JSONResponse:
        try:
            upstream = await call(RECS)
        except (BulkheadFullError, httpx.TimeoutException) as exc:
            return JSONResponse({"recs": [], "degraded": type(exc).__name__})
        return JSONResponse({"recs": upstream.json()["items"], "degraded": None})

    async def checkout(request: Request) -> JSONResponse:
        try:
            upstream = await call(PAYMENTS)
        except (BulkheadFullError, httpx.TimeoutException) as exc:
            return JSONResponse({"charged": False, "error": type(exc).__name__}, status_code=503)
        return JSONResponse({"charged": upstream.json()["ok"]})

    return Starlette(routes=[Route("/home", home), Route("/checkout", checkout, methods=["POST"])])


def shuffle_shard(tenant: str, *, workers: int, shard_size: int) -> tuple[int, ...]:
    """Pick ``shard_size`` of ``workers`` by rendezvous (highest-random-weight) hashing."""
    if not 1 <= shard_size <= workers:
        raise ValueError("need 1 <= shard_size <= workers")

    def weight(worker: int) -> bytes:
        return hashlib.sha256(f"{tenant}/{worker}".encode()).digest()

    return tuple(sorted(sorted(range(workers), key=weight, reverse=True)[:shard_size]))


def shard_overlap_histogram(
    *, workers: int, shard_size: int, shard: tuple[int, ...]
) -> dict[int, int]:
    """For every possible shard, count how many workers it shares with ``shard``."""
    target = set(shard)
    counts = Counter(
        len(target.intersection(other))
        for other in itertools.combinations(range(workers), shard_size)
    )
    return dict(sorted(counts.items(), reverse=True))
