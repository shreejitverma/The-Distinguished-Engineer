"""Cache stampede labs: singleflight coalescing, memcache leases, and XFetch early recompute."""

from __future__ import annotations

import asyncio
import gc
import itertools
import math
import random
import statistics
from typing import Any

import httpx
import pytest

from api_labs.f03_http_caching_and_conditional_requests.cache_stampede import (
    Hit,
    LeaseCache,
    LeaseGranted,
    RetryLater,
    SingleFlight,
    TTLCache,
    build_app,
    simulate_cycle,
    unit_interval_open_at_zero,
    xfetch_should_recompute,
)

CLIENTS = 100


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class GatedDatabase:
    """A loader that blocks on `release` so a test can pile up concurrent misses first."""

    def __init__(self) -> None:
        self.calls = 0
        self.in_progress = 0
        self.release = asyncio.Event()

    async def load(self, key: str) -> dict[str, Any]:
        self.calls += 1
        version = self.calls
        self.in_progress += 1
        try:
            await self.release.wait()
            return {"key": key, "version": version}
        finally:
            self.in_progress -= 1


async def wait_until(predicate: Any, *, max_spins: int = 100_000) -> None:
    for _ in range(max_spins):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def stampede(*, coalesce: bool) -> tuple[GatedDatabase, list[httpx.Response]]:
    """Expire one key, fire CLIENTS concurrent GETs at it, then let the database answer."""
    db = GatedDatabase()
    clock = FakeClock()
    cache: TTLCache[dict[str, Any]] = TTLCache(db.load, ttl=30.0, clock=clock, coalesce=coalesce)
    cache.put("item:42", {"key": "item:42", "version": 0})
    clock.now = 31.0  # the entry is now expired
    app = build_app(cache)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        requests = [asyncio.create_task(client.get("/items/42")) for _ in range(CLIENTS)]
        if coalesce:
            await wait_until(lambda: cache.flight.waiters("item:42") == CLIENTS)
        else:
            await wait_until(lambda: db.in_progress == CLIENTS)
        db.release.set()
        responses = await asyncio.gather(*requests)
    return db, responses


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uncoalesced_expiry_sends_every_concurrent_miss_to_the_database() -> None:
    db, responses = await stampede(coalesce=False)

    assert db.calls == CLIENTS
    assert {r.status_code for r in responses} == {200}
    assert {r.json()["version"] for r in responses} == set(range(1, CLIENTS + 1))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_singleflight_collapses_100_concurrent_misses_into_one_query() -> None:
    db, responses = await stampede(coalesce=True)

    assert db.calls == 1
    assert {r.status_code for r in responses} == {200}
    assert {r.content for r in responses} == {b'{"key":"item:42","version":1}'}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_joined_flag_is_false_for_the_leader_and_true_for_followers() -> None:
    flight: SingleFlight[str, int] = SingleFlight()
    gate = asyncio.Event()

    async def load() -> int:
        await gate.wait()
        return 7

    calls = [asyncio.create_task(flight.do("k", load)) for _ in range(3)]
    await wait_until(lambda: flight.waiters("k") == 3)
    gate.set()

    assert await asyncio.gather(*calls) == [(7, False), (7, True), (7, True)]
    assert flight.waiters("k") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_is_shared_then_forgotten_so_the_next_miss_retries() -> None:
    flight: SingleFlight[str, str] = SingleFlight()
    attempts = 0
    gate = asyncio.Event()

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        await gate.wait()
        if attempts == 1:
            raise ConnectionError("database down")
        return "recovered"

    calls = [asyncio.create_task(flight.do("k", flaky)) for _ in range(5)]
    await wait_until(lambda: flight.waiters("k") == 5)
    gate.set()
    results = await asyncio.gather(*calls, return_exceptions=True)

    assert attempts == 1
    assert all(isinstance(r, ConnectionError) for r in results)
    assert not flight.in_flight("k")
    assert await flight.do("k", flaky) == ("recovered", False)
    assert attempts == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_old_call_finishing_after_forget_does_not_drop_the_new_call() -> None:
    flight: SingleFlight[str, str] = SingleFlight()
    old_gate, new_gate = asyncio.Event(), asyncio.Event()
    loads: list[str] = []

    async def load_old() -> str:
        loads.append("old")
        await old_gate.wait()
        return "old"

    async def load_new() -> str:
        loads.append("new")
        await new_gate.wait()
        return "new"

    async def load_third() -> str:
        loads.append("third")
        return "third"

    old = asyncio.create_task(flight.do("k", load_old))
    await wait_until(lambda: loads == ["old"])
    flight.forget("k")
    new = asyncio.create_task(flight.do("k", load_new))
    await wait_until(lambda: loads == ["old", "new"])
    old_gate.set()
    assert await old == ("old", False)

    # The identity check (Go's `g.m[key] == c`) keeps the old call from deleting the new one.
    assert flight.in_flight("k")
    joiner = asyncio.create_task(flight.do("k", load_third))
    await wait_until(lambda: flight.waiters("k") == 2)
    new_gate.set()

    assert [await new, await joiner] == [("new", False), ("new", True)]
    assert loads == ["old", "new"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelling_the_leader_caller_does_not_cancel_the_shared_load() -> None:
    flight: SingleFlight[str, str] = SingleFlight()
    gate = asyncio.Event()
    load_cancelled = False

    async def load() -> str:
        nonlocal load_cancelled
        try:
            await gate.wait()
        except asyncio.CancelledError:
            load_cancelled = True
            raise
        return "row"

    leader = asyncio.create_task(flight.do("k", load))
    followers = [asyncio.create_task(flight.do("k", load)) for _ in range(4)]
    await wait_until(lambda: flight.waiters("k") == 5)
    leader.cancel()
    await wait_until(lambda: flight.waiters("k") == 4)
    gate.set()

    assert await asyncio.gather(*followers) == [("row", True)] * 4
    assert leader.cancelled()
    assert not load_cancelled


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_abandoned_by_every_caller_still_warms_the_cache() -> None:
    db = GatedDatabase()
    cache: TTLCache[dict[str, Any]] = TTLCache(db.load, ttl=30.0, clock=FakeClock(), coalesce=True)

    caller = asyncio.create_task(cache.get("item:1"))
    await wait_until(lambda: db.in_progress == 1)
    caller.cancel()
    await wait_until(caller.done)
    db.release.set()
    await wait_until(lambda: not cache.flight.in_flight("item:1"))

    assert await cache.get("item:1") == {"key": "item:1", "version": 1}
    assert db.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_of_a_load_every_caller_abandoned_is_reported_not_silent() -> None:
    flight: SingleFlight[str, str] = SingleFlight()
    gate = asyncio.Event()
    reported: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: reported.append(ctx))

    async def load() -> str:
        await gate.wait()
        raise ConnectionError("database down")

    caller = asyncio.create_task(flight.do("k", load))
    await wait_until(lambda: flight.waiters("k") == 1)
    caller.cancel()
    await wait_until(caller.done)
    gate.set()
    await wait_until(lambda: not flight.in_flight("k"))
    del caller
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()

    # 3.14.6 and 3.15.0b3 report "ConnectionError exception in shielded future" eagerly;
    # 3.12.13, 3.13.18, 3.14.10, and 3.15.2 report "Task exception was never retrieved" at GC.
    assert [type(ctx["exception"]) for ctx in reported] == [ConnectionError]
    assert reported[0]["message"] in {
        "ConnectionError exception in shielded future",
        "Task exception was never retrieved",
    }


@pytest.mark.unit
def test_lease_lets_one_client_fill_and_tells_the_herd_to_retry() -> None:
    clock = FakeClock(100.0)
    cache: LeaseCache[str] = LeaseCache(clock=clock)

    first = cache.get("user:9")
    herd = [cache.get("user:9") for _ in range(99)]
    clock.now = 109.999
    late = cache.get("user:9")

    assert first == LeaseGranted(token=1)
    assert herd == [RetryLater(stale=None)] * 99
    assert late == RetryLater(stale=None)
    assert cache.set("user:9", "row-v1", token=1)
    assert cache.get("user:9") == Hit("row-v1")


@pytest.mark.unit
def test_delete_invalidates_the_outstanding_lease_to_prevent_a_stale_set() -> None:
    clock = FakeClock()
    cache: LeaseCache[str] = LeaseCache(clock=clock)

    granted = cache.get("user:9")
    assert isinstance(granted, LeaseGranted)
    cache.delete("user:9")  # a writer committed v2 while the reader held v1

    assert not cache.set("user:9", "row-v1-stale", token=granted.token)
    assert isinstance(cache.get("user:9"), LeaseGranted)


@pytest.mark.unit
def test_retry_later_carries_the_deleted_value_as_stale() -> None:
    clock = FakeClock()
    cache: LeaseCache[str] = LeaseCache(clock=clock)
    granted = cache.get("k")
    assert isinstance(granted, LeaseGranted)
    assert cache.set("k", "v1", token=granted.token)

    cache.delete("k")
    holder = cache.get("k")
    waiter = cache.get("k")

    assert isinstance(holder, LeaseGranted)
    assert waiter == RetryLater(stale="v1")


@pytest.mark.unit
def test_lease_rate_limit_reissues_after_the_interval_and_rejects_the_old_token() -> None:
    clock = FakeClock(0.0)
    cache: LeaseCache[str] = LeaseCache(clock=clock, lease_interval=10.0)

    assert cache.get("k") == LeaseGranted(token=1)  # holder then crashes
    clock.now = 10.0
    assert cache.get("k") == LeaseGranted(token=2)
    assert not cache.set("k", "from-crashed-holder", token=1)
    assert cache.set("k", "fresh", token=2)


@pytest.mark.unit
def test_random_random_can_return_zero_which_log_rejects() -> None:
    # CPython 3.14 reworded this error; older releases say "math domain error".
    with pytest.raises(ValueError, match=r"math domain error|expected a positive input"):
        math.log(0.0)
    assert unit_interval_open_at_zero(lambda: 0.0) == 1.0
    with pytest.raises(ValueError, match=r"\(0, 1\]"):
        xfetch_should_recompute(now=0.0, expiry=1.0, delta=1.0, beta=1.0, rand=lambda: 0.0)


@pytest.mark.unit
def test_xfetch_always_recomputes_at_or_after_expiry_and_never_early_with_beta_zero() -> None:
    assert xfetch_should_recompute(now=60.0, expiry=60.0, delta=2.0, beta=1.0, rand=lambda: 1.0)
    for u in (1e-12, 0.5, 1.0):
        assert not xfetch_should_recompute(
            now=59.999, expiry=60.0, delta=2.0, beta=0.0, rand=itertools.repeat(u).__next__
        )


@pytest.mark.unit
def test_xfetch_early_probability_is_exp_of_minus_gap_over_delta_beta() -> None:
    n = 10_000
    grid = [(i + 0.5) / n for i in range(n)]
    delta, beta, expiry = 0.2, 1.5, 100.0
    for gap in (0.05, 0.3, 0.9):
        draws = iter(grid).__next__
        hits = sum(
            xfetch_should_recompute(
                now=expiry - gap, expiry=expiry, delta=delta, beta=beta, rand=draws
            )
            for _ in range(n)
        )
        expected = math.exp(-gap / (delta * beta))
        assert abs(hits / n - expected) <= 1 / n


@pytest.mark.unit
def test_xfetch_shrinks_the_stampede_from_n_to_a_constant_with_a_log_n_early_gap() -> None:
    rng = random.Random(20261109)  # noqa: S311 - simulation, not security
    n, cycles = 100, 300

    naive = [
        simulate_cycle(arrivals_per_delta=n, ttl_in_deltas=30.0, beta=0.0, rand=lambda: 1.0)
        for _ in range(cycles)
    ]
    xfetch = [
        simulate_cycle(
            arrivals_per_delta=n,
            ttl_in_deltas=30.0,
            beta=1.0,
            rand=lambda: unit_interval_open_at_zero(rng.random),
        )
        for _ in range(cycles)
    ]

    assert {s.size for s in naive} == {n}
    assert {s.early_gap for s in naive} == {0.0}
    mean_size = statistics.fmean(s.size for s in xfetch)
    mean_gap = statistics.fmean(s.early_gap for s in xfetch)
    # Vattani et al. Theorem 7, lambda = 1: E[S] -> (e - 1)(1 + 1/e) ~ 2.35, E[T] -> ln n ~ 4.6.
    assert 1.5 < mean_size < 3.5
    assert 3.0 < mean_gap < 6.0
