"""Unit tests: a hung dependency drains a shared pool; per-dependency bulkheads contain it."""

import math
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import anyio
import anyio.to_thread
import httpx
import pytest
from starlette.types import ASGIApp

from api_labs.f16_resilience_rate_limiting_and_idempotency.bulkhead_isolation import (
    PAYMENTS,
    RECS,
    Bulkhead,
    BulkheadFullError,
    Downstream,
    InMemoryNetwork,
    TenantBulkheads,
    TierPolicy,
    build_storefront,
    partitioned_client,
    shard_overlap_histogram,
    shared_client,
    shuffle_shard,
)

WAIT_LIMIT_S = 5.0


async def wait_until(predicate: Callable[[], bool]) -> None:
    with anyio.fail_after(WAIT_LIMIT_S):
        for _ in range(1_000_000):
            if predicate():
                return
            await anyio.sleep(0.001)


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def network_with_hung_recs() -> tuple[InMemoryNetwork, Downstream, Downstream]:
    recs = Downstream(b'{"items": ["a", "b"]}', hang=True)
    payments = Downstream(b'{"ok": true}')
    return InMemoryNetwork({RECS: recs, PAYMENTS: payments}), recs, payments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hung_recs_exhaust_shared_pool_and_checkout_fails_with_pool_timeout() -> None:
    network, recs, payments = network_with_hung_recs()
    upstream = shared_client(network, max_connections=4, pool_timeout=0.05)
    homes: list[dict[str, Any]] = []

    async with (
        upstream,
        client_for(build_storefront(upstream)) as client,
        anyio.create_task_group() as tg,
    ):

        async def home() -> None:
            homes.append((await client.get("/home")).json())

        for _ in range(4):
            tg.start_soon(home)
        await wait_until(lambda: recs.waiting_at_gate == 4)

        checkout = await client.post("/checkout")
        assert checkout.status_code == 503
        assert checkout.json() == {"charged": False, "error": "PoolTimeout"}
        assert payments.requests_seen == 0  # the healthy dependency was never reached
        assert recs.open_connections == 4

        recs.gate.set()

    assert homes == [{"recs": ["a", "b"], "degraded": None}] * 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_dependency_pools_keep_checkout_alive_while_recs_hang() -> None:
    network, recs, payments = network_with_hung_recs()
    upstream = partitioned_client(network, limits={RECS: 4, PAYMENTS: 2}, pool_timeout=0.05)
    degraded: list[str | None] = []

    async with (
        upstream,
        client_for(build_storefront(upstream)) as client,
        anyio.create_task_group() as tg,
    ):

        async def home() -> None:
            degraded.append((await client.get("/home")).json()["degraded"])

        for _ in range(6):
            tg.start_soon(home)
        await wait_until(lambda: recs.waiting_at_gate == 4 and len(degraded) == 2)
        assert degraded == ["PoolTimeout", "PoolTimeout"]  # contained inside the recs pool

        for _ in range(3):
            checkout = await client.post("/checkout")
            assert checkout.status_code == 200
            assert checkout.json() == {"charged": True}
        assert payments.requests_seen == 3
        assert payments.peak_connections == 1  # sequential calls reuse one keep-alive conn
        assert recs.peak_connections == 4

        recs.gate.set()

    assert degraded[2:] == [None] * 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulkhead_rejects_excess_immediately_where_pool_alone_makes_them_wait() -> None:
    async def run(bulkhead: Bulkhead | None) -> list[str | None]:
        network, recs, _ = network_with_hung_recs()
        upstream = partitioned_client(network, limits={RECS: 4, PAYMENTS: 2}, pool_timeout=0.05)
        heads = {RECS: bulkhead} if bulkhead else None
        outcomes: list[str | None] = []
        async with (
            upstream,
            client_for(build_storefront(upstream, heads)) as client,
            anyio.create_task_group() as tg,
        ):

            async def home() -> None:
                outcomes.append((await client.get("/home")).json()["degraded"])

            for _ in range(10):
                tg.start_soon(home)
            await wait_until(lambda: recs.waiting_at_gate == 4 and len(outcomes) == 6)
            assert recs.requests_seen == 4
            recs.gate.set()
        return outcomes

    assert (await run(None))[:6] == ["PoolTimeout"] * 6  # each excess caller waited 50 ms
    head = Bulkhead(RECS, max_concurrent=4, max_wait=0.0)
    assert (await run(head))[:6] == ["BulkheadFullError"] * 6  # rejected with zero wait
    assert (head.admitted, head.rejected, head.in_use) == (4, 6, 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounded_wait_admits_in_fifo_order_and_timed_out_waiter_leaves_no_trace() -> None:
    head = Bulkhead("ledger", max_concurrent=1, max_wait=1.0)
    order: list[str] = []
    release = anyio.Event()

    async def holder() -> None:
        async with head.slot():
            await release.wait()

    async def waiter(name: str) -> None:
        async with head.slot():
            order.append(name)

    async with anyio.create_task_group() as tg:
        tg.start_soon(holder)
        await wait_until(lambda: head.in_use == 1)
        tg.start_soon(waiter, "first")
        await wait_until(lambda: head.waiting == 1)
        tg.start_soon(waiter, "second")
        await wait_until(lambda: head.waiting == 2)
        release.set()
    assert order == ["first", "second"]

    short = Bulkhead("ledger", max_concurrent=1, max_wait=0.05)
    async with short.slot():
        with pytest.raises(BulkheadFullError, match="'ledger' is full"):
            async with short.slot():
                pytest.fail("admitted past a full bulkhead")
        assert (short.in_use, short.waiting) == (1, 0)
    async with short.slot():
        assert short.in_use == 1
    assert (short.admitted, short.rejected, short.in_use) == (2, 1, 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tenant_tiers_reserve_capacity_and_cap_each_noisy_tenant() -> None:
    heads = TenantBulkheads(
        {"enterprise": TierPolicy(tier_limit=3, tenant_limit=3), "free": TierPolicy(2, 1)}
    )

    async def rejected_by(tenant: str, tier: str, stack: AsyncExitStack) -> str | None:
        try:
            await stack.enter_async_context(heads.slot(tenant, tier))
        except BulkheadFullError as exc:
            return exc.bulkhead
        return None

    async with AsyncExitStack() as stack:
        assert await rejected_by("free-a", "free", stack) is None
        assert await rejected_by("free-a", "free", stack) == "tenant:free:free-a"
        assert await rejected_by("free-b", "free", stack) is None
        assert await rejected_by("free-c", "free", stack) == "tier:free"
        assert heads.tracked_tenants == 2  # free-c's tenant slot was released and evicted
        for _ in range(3):
            assert await rejected_by("acme", "enterprise", stack) is None
        assert await rejected_by("acme", "enterprise", stack) == "tenant:enterprise:acme"
        assert heads.tier("free").in_use == 2
        assert heads.tier("enterprise").in_use == 3

    assert heads.tracked_tenants == 0
    assert heads.tier("free").in_use == heads.tier("enterprise").in_use == 0
    with pytest.raises(KeyError):
        await rejected_by("mallory", "platinum", AsyncExitStack())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedicated_thread_limiter_leaves_the_default_40_tokens_untouched() -> None:
    sdk_limiter = anyio.CapacityLimiter(2)
    default = anyio.to_thread.current_default_thread_limiter()
    unblock = threading.Event()

    async def blocking_sdk_call() -> None:
        await anyio.to_thread.run_sync(unblock.wait, WAIT_LIMIT_S, limiter=sdk_limiter)

    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(blocking_sdk_call)
        await wait_until(
            lambda: sdk_limiter.borrowed_tokens == 2 and sdk_limiter.statistics().tasks_waiting == 1
        )
        assert default.total_tokens == 40
        assert default.borrowed_tokens == 0
        assert await anyio.to_thread.run_sync(threading.get_ident) != threading.get_ident()
        unblock.set()
    assert sdk_limiter.borrowed_tokens == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raw_capacity_limiter_is_per_task_but_bulkhead_counts_calls() -> None:
    limiter = anyio.CapacityLimiter(2)
    async with limiter:
        with pytest.raises(RuntimeError, match="already holding"):
            await limiter.acquire()

    head = Bulkhead("fan-out", max_concurrent=2)
    async with head.slot(), head.slot():
        assert head.in_use == 2
        with pytest.raises(BulkheadFullError):
            async with head.slot():
                pytest.fail("third slot admitted")
    assert head.in_use == 0


@pytest.mark.unit
def test_shuffle_shard_combinatorics_match_the_builders_library_numbers() -> None:
    assert math.comb(8, 2) == 28
    assert math.comb(2048, 4) == 730_862_190_080
    assert shard_overlap_histogram(workers=8, shard_size=2, shard=(0, 1)) == {2: 1, 1: 12, 0: 15}

    shard = shuffle_shard("tenant-42", workers=8, shard_size=2)
    assert shard == shuffle_shard("tenant-42", workers=8, shard_size=2)
    assert len(set(shard)) == 2
    assert all(0 <= worker < 8 for worker in shard)
    for n in range(200):
        before = set(shuffle_shard(f"t{n}", workers=8, shard_size=2))
        after = set(shuffle_shard(f"t{n}", workers=9, shard_size=2))
        assert after <= before | {8}  # adding worker 8 moves a tenant only onto worker 8
    with pytest.raises(ValueError, match="shard_size"):
        shuffle_shard("t", workers=2, shard_size=3)
