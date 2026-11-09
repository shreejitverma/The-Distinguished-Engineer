"""Outbound pooling in httpx 0.28.1 / httpcore 1.0.9: limits, idle expiry, and DNS staleness.

Every pooling decision below is made by the pinned httpcore code; only the
socket and the resolver are simulated (see `outbound_pool.py`). One manual
clock drives DNS TTLs, httpcore's keep-alive expiry, and connection lifetime.
"""

from __future__ import annotations

import importlib
import inspect
import types
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest
from asgi_lifespan import LifespanManager

from api_labs.f02_tls_connections_and_intermediaries.outbound_pool import (
    UPSTREAM,
    BackendTransport,
    FakeDNS,
    ManualClock,
    MaxLifetimeTransport,
    RecordingBackend,
    build_gateway,
    make_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

HOST = "api.internal"
OLD = "10.0.0.1"
NEW = "10.0.0.2"
TTL = 60.0
WAIT = 2.0  # real-time bound on waits for in-process events


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    """A manual clock, also installed as the monotonic clock httpcore's HTTP/1.1 code reads."""
    manual = ManualClock()
    http11 = importlib.import_module("httpcore._async.http11")
    monkeypatch.setattr(http11, "time", types.SimpleNamespace(monotonic=manual))
    return manual


@pytest.fixture
def dns(clock: ManualClock) -> FakeDNS:
    resolver = FakeDNS(clock)
    resolver.publish(HOST, OLD, ttl=TTL)
    return resolver


@pytest.fixture
def backend(dns: FakeDNS) -> RecordingBackend:
    return RecordingBackend(dns)


async def burst(
    send: list[tuple[str, httpx.AsyncClient]],
    alongside: Callable[[], Coroutine[Any, Any, None]],
) -> tuple[list[str], list[type[BaseException]]]:
    """Fire every (path, client) request concurrently next to `alongside`; collect results."""
    bodies: list[str] = []
    errors: list[type[BaseException]] = []

    async def one(path: str, client: httpx.AsyncClient) -> None:
        try:
            bodies.append((await client.get(path)).text)
        except httpx.HTTPError as exc:
            errors.append(type(exc))

    async with anyio.create_task_group() as tg:
        tg.start_soon(alongside)
        for path, client in send:
            tg.start_soon(one, path, client)
    return bodies, errors


@pytest.mark.unit
def test_httpx_default_limits_and_timeouts() -> None:
    transport_limits = inspect.signature(httpx.AsyncHTTPTransport.__init__).parameters["limits"]
    assert transport_limits.default == httpx.Limits(
        max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0
    )
    client_timeout = inspect.signature(httpx.AsyncClient.__init__).parameters["timeout"]
    assert client_timeout.default == httpx.Timeout(5.0)
    assert httpx.Timeout(5.0).pool == 5.0
    assert issubclass(httpx.PoolTimeout, httpx.TimeoutException)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_client_reuses_one_connection(backend: RecordingBackend) -> None:
    async with make_client(backend, httpx.Limits()) as client:
        for _ in range(5):
            assert (await client.get("/v1/price")).text == OLD
    assert backend.connects == 1
    assert backend.streams[0].requests_served == 5
    assert backend.open_now == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_request_clients_open_one_connection_each(backend: RecordingBackend) -> None:
    for _ in range(5):
        async with make_client(backend, httpx.Limits()) as client:
            assert (await client.get("/v1/price")).text == OLD
    assert backend.connects == 5
    assert all(stream.requests_served == 1 for stream in backend.streams)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unclosed_per_request_clients_leak_connections(backend: RecordingBackend) -> None:
    for _ in range(5):
        client = make_client(backend, httpx.Limits())
        await client.get("/v1/price")
    assert backend.open_now == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_burst_beyond_max_connections_raises_pool_timeout(
    backend: RecordingBackend,
) -> None:
    gate = backend.hold("/slow")
    limits = httpx.Limits(max_connections=2)
    bodies: list[str] = []
    errors: list[type[BaseException]] = []
    three_timed_out = anyio.Event()

    async with make_client(backend, limits, pool_timeout=0.05) as client:

        async def one() -> None:
            try:
                bodies.append((await client.get("/slow")).text)
            except httpx.HTTPError as exc:
                errors.append(type(exc))
                if len(errors) == 3:
                    three_timed_out.set()

        async def release() -> None:
            with anyio.fail_after(WAIT):  # a broken mechanism fails instead of hanging
                await three_timed_out.wait()  # the 3 queued requests gave up first
            gate.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(release)
            for _ in range(5):
                tg.start_soon(one)
    assert bodies == [OLD, OLD]
    assert errors == [httpx.PoolTimeout] * 3
    assert backend.connects == 2
    assert backend.peak_open == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_request_clients_ignore_the_limit_under_the_same_burst(
    backend: RecordingBackend,
) -> None:
    gate = backend.hold("/slow")
    limits = httpx.Limits(max_connections=2)
    clients = [make_client(backend, limits, pool_timeout=0.05) for _ in range(5)]

    async def release() -> None:
        with anyio.fail_after(WAIT):  # a broken mechanism fails instead of hanging
            await backend.wait_for_requests(5)
        gate.set()

    bodies, errors = await burst([("/slow", c) for c in clients], release)
    for client in clients:
        await client.aclose()
    assert errors == []
    assert bodies == [OLD] * 5
    assert backend.peak_open == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_keepalive_closes_surplus_idle_connections(backend: RecordingBackend) -> None:
    gate = backend.hold("/slow")
    limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)
    async with make_client(backend, limits) as client:

        async def release() -> None:
            with anyio.fail_after(WAIT):  # a broken mechanism fails instead of hanging
                await backend.wait_for_requests(5)
            gate.set()

        _, errors = await burst([("/slow", client)] * 5, release)
        assert errors == []
        assert backend.connects == 5
        assert backend.open_now == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpcore_1_0_9_counts_busy_connections_against_max_keepalive(
    backend: RecordingBackend,
) -> None:
    gate = backend.hold("/slow")
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=1)
    async with make_client(backend, limits) as client, anyio.create_task_group() as tg:
        tg.start_soon(client.get, "/slow")
        tg.start_soon(client.get, "/slow")
        with anyio.fail_after(WAIT):  # a broken mechanism fails instead of hanging
            await backend.wait_for_requests(2)
        await client.get("/fast")
        # One idle connection is within max_keepalive_connections=1, yet it was
        # closed: the pool compared the total connection count (3) to the limit.
        assert backend.open_now == 2
        await client.get("/fast")
        assert backend.connects == 4
        gate.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_busy_connection_pins_old_address_past_dns_ttl(
    clock: ManualClock, dns: FakeDNS, backend: RecordingBackend
) -> None:
    async with make_client(backend, httpx.Limits()) as client:
        served = [(await client.get("/v1/price")).text]
        dns.publish(HOST, NEW, ttl=TTL)
        for _ in range(20):  # one request every 4 s: never idle past keepalive_expiry=5.0
            clock.advance(4.0)
            served.append((await client.get("/v1/price")).text)
    assert clock.now == 80.0  # 20 s past the 60 s TTL of the first answer
    assert served == [OLD] * 21
    assert backend.connects == 1
    assert dns.upstream_queries == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idle_expiry_reconnects_and_re_resolves(
    clock: ManualClock, dns: FakeDNS, backend: RecordingBackend
) -> None:
    limits = httpx.Limits(keepalive_expiry=5.0)
    async with make_client(backend, limits) as client:
        assert (await client.get("/v1/price")).text == OLD
        dns.publish(HOST, NEW, ttl=TTL)
        clock.advance(10.0)  # idle past keepalive_expiry, DNS cache still valid
        assert (await client.get("/v1/price")).text == OLD
        clock.advance(TTL)  # idle again, and the cached record has expired
        assert (await client.get("/v1/price")).text == NEW
    assert backend.connects == 3
    assert dns.upstream_queries == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_connection_close_after_n_requests_forces_re_resolution(
    clock: ManualClock, dns: FakeDNS, backend: RecordingBackend
) -> None:
    backend.max_requests_per_connection = 20
    async with make_client(backend, httpx.Limits()) as client:
        responses = [await client.get("/v1/price")]
        dns.publish(HOST, NEW, ttl=TTL)
        for _ in range(20):  # busy: one request every 4 s, t = 4 .. 80
            clock.advance(4.0)
            responses.append(await client.get("/v1/price"))
    assert [r.text for r in responses] == [OLD] * 20 + [NEW]
    assert responses[19].headers["connection"] == "close"
    assert "connection" not in responses[18].headers
    assert backend.connects == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_lifetime_transport_rotates_and_drains(
    clock: ManualClock, dns: FakeDNS, backend: RecordingBackend
) -> None:
    gate = backend.hold("/slow")
    transport = MaxLifetimeTransport(
        lambda: BackendTransport(backend, httpx.Limits()), max_lifetime=TTL, clock=clock
    )
    async with httpx.AsyncClient(transport=transport, base_url=UPSTREAM) as client:
        slow_bodies: list[str] = []

        async def slow() -> None:
            slow_bodies.append((await client.get("/slow")).text)

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow)
            with anyio.fail_after(WAIT):  # a broken mechanism fails instead of hanging
                await backend.wait_for_requests(1)
            dns.publish(HOST, NEW, ttl=TTL)
            served: list[str] = []
            for _ in range(16):  # busy: one request every 4 s, t = 4 .. 64
                clock.advance(4.0)
                served.append((await client.get("/v1/price")).text)
            assert served == [OLD] * 14 + [NEW] * 2
            assert backend.open_to(OLD) == 2  # the retired pool still owns the /slow request
            gate.set()
        assert slow_bodies == [OLD]
        assert backend.open_to(OLD) == 0
        assert backend.open_to(NEW) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_lifetime_transport_closes_an_idle_retired_pool_at_rotation(
    clock: ManualClock, dns: FakeDNS, backend: RecordingBackend
) -> None:
    transport = MaxLifetimeTransport(
        lambda: BackendTransport(backend, httpx.Limits()), max_lifetime=TTL, clock=clock
    )
    async with httpx.AsyncClient(transport=transport, base_url=UPSTREAM) as client:
        served = [(await client.get("/v1/price")).text]
        dns.publish(HOST, NEW, ttl=TTL)
        for _ in range(15):  # busy: one request every 4 s, t = 4 .. 60
            clock.advance(4.0)
            served.append((await client.get("/v1/price")).text)
        assert served == [OLD] * 15 + [NEW]
        # Nothing was in flight on the retired pool, so no response close will
        # ever release it; rotation itself must close its idle connection.
        assert backend.open_to(OLD) == 0
        assert backend.open_to(NEW) == 1
    assert backend.connects == 2


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("shared", "expected_connects"), [(True, 1), (False, 10)])
async def test_gateway_lifespan_client_vs_per_request_client(
    backend: RecordingBackend, shared: bool, expected_connects: int
) -> None:
    app = build_gateway(backend, httpx.Limits(), shared=shared)
    async with (
        LifespanManager(app) as manager,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://test"
        ) as inbound,
    ):
        for _ in range(10):
            response = await inbound.get("/proxy")
            assert response.json() == {"served_by": OLD}
        assert backend.connects == expected_connects
    assert backend.open_now == 0
