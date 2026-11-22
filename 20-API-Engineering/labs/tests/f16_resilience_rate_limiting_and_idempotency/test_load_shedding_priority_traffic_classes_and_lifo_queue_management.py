"""Lab: prioritized load shedding, CoDel queue timeout, and LIFO vs FIFO under saturation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from api_labs.f16_resilience_rate_limiting_and_idempotency.load_shedding import (
    OVERLOAD,
    POLICIES,
    CoDelTimeout,
    Policy,
    Priority,
    PriorityShedder,
    SimResult,
    Workload,
    simulate,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import Receive, Scope, Send

BY_NAME = {p.name: p for p in POLICIES}


# --- CoDel controller (folly service-queue variant) -------------------------------


@pytest.mark.unit
def test_codel_tolerates_a_burst_that_drains_within_the_interval() -> None:
    codel = CoDelTimeout(target=0.005, interval=0.100)
    drops: list[bool] = []
    for step in range(40):  # 400 ms of samples every 10 ms
        now = step * 0.010
        sojourn = 0.050 if step % 10 else 0.001  # one short-wait sample per interval
        drops.append(codel.should_drop(sojourn, now))
    assert not any(drops)
    assert codel.overloaded is False


@pytest.mark.unit
def test_codel_sheds_only_requests_older_than_twice_target_once_queue_stands() -> None:
    codel = CoDelTimeout(target=0.005, interval=0.100)
    for step in range(25):  # min sojourn stays 8 ms > 5 ms target for > 1 interval
        codel.should_drop(0.008 + (step % 3) * 0.001, step * 0.010)
    assert codel.overloaded is True
    assert codel.should_drop(0.009, 0.255) is False  # 9 ms <= slough timeout of 10 ms
    assert codel.should_drop(0.011, 0.256) is True  # 11 ms > 2 x target


@pytest.mark.unit
def test_codel_leaves_overload_after_an_interval_whose_minimum_is_below_target() -> None:
    codel = CoDelTimeout(target=0.005, interval=0.100)
    for step in range(25):
        codel.should_drop(0.020, step * 0.010)
    assert codel.overloaded is True
    for step in range(25, 50):
        codel.should_drop(0.001, step * 0.010)
    assert codel.overloaded is False
    assert codel.should_drop(0.500, 0.505) is False


@pytest.mark.unit
def test_codel_rejects_target_not_below_interval() -> None:
    with pytest.raises(ValueError, match="target < interval"):
        CoDelTimeout(target=0.100, interval=0.100)


# --- Discrete-event overload model ------------------------------------------------


@pytest.mark.unit
def test_unbounded_fifo_collapses_and_stays_collapsed_after_the_spike() -> None:
    result = simulate(BY_NAME["fifo_unbounded"], OVERLOAD)
    overall = result.summary()
    recovery = result.summary(since=OVERLOAD.phases[0][0])
    assert overall.p95 > OVERLOAD.client_timeout * 4
    assert overall.success_ratio < 0.25
    assert recovery.success_ratio < 0.25  # load fell to 50% yet the standing queue persists
    assert result.wasted_server_seconds > 100.0


@pytest.mark.unit
def test_queue_timeout_alone_restores_recovery_but_serves_at_the_deadline() -> None:
    result = simulate(BY_NAME["fifo_queue_timeout"], OVERLOAD)
    assert result.summary(since=OVERLOAD.phases[0][0]).success_ratio > 0.99
    assert result.summary().p50 > 0.9 * OVERLOAD.client_timeout
    assert result.summary().late > 1000


@pytest.mark.unit
@pytest.mark.parametrize("name", ["fifo_codel", "adaptive_lifo_codel", "priority_lifo_codel"])
def test_short_sojourn_timeout_keeps_p95_far_below_client_timeout(name: str) -> None:
    result = simulate(BY_NAME[name], OVERLOAD)
    overall = result.summary()
    capacity_share = (10 * OVERLOAD.capacity_rps + 10 * 200) / overall.offered
    assert overall.p95 < 0.2 * OVERLOAD.client_timeout
    assert overall.late == 0
    assert overall.success_ratio > 0.95 * capacity_share


@pytest.mark.unit
def test_priority_admission_protects_critical_and_sheds_background_first() -> None:
    blind = simulate(BY_NAME["adaptive_lifo_codel"], OVERLOAD)
    prio = simulate(BY_NAME["priority_lifo_codel"], OVERLOAD)
    assert blind.summary(Priority.CRITICAL).success_ratio < 0.80
    assert prio.summary(Priority.CRITICAL).success_ratio > 0.97
    assert prio.summary(Priority.BACKGROUND).rejected > 0
    assert prio.summary(Priority.CRITICAL).rejected == 0


@pytest.mark.unit
def test_lifo_and_fifo_have_equal_mean_wait_but_different_tails() -> None:
    stable = Workload(phases=((200.0, 45.0),), servers=1, mean_service=0.020, seed=3)
    fifo = simulate(Policy("fifo"), stable)
    lifo = simulate(Policy("lifo", order="lifo"), stable)

    def waits(r: SimResult) -> list[float]:
        return [o.latency for o in r.outcomes if o.latency is not None]

    fifo_lat, lifo_lat = waits(fifo), waits(lifo)
    assert len(fifo_lat) == len(lifo_lat) == len(stable.arrivals())
    assert sum(fifo_lat) / len(fifo_lat) == pytest.approx(sum(lifo_lat) / len(lifo_lat), rel=1e-9)
    assert lifo.summary().p50 < fifo.summary().p50
    assert lifo.summary().p99 > 1.5 * fifo.summary().p99


@pytest.mark.unit
def test_simulation_is_deterministic_for_a_seed() -> None:
    a = simulate(BY_NAME["priority_lifo_codel"], OVERLOAD)
    b = simulate(BY_NAME["priority_lifo_codel"], OVERLOAD)
    assert a == b


# --- ASGI middleware --------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def classify(scope: Scope) -> Priority:
    path: str = scope["path"]
    return Priority.BACKGROUND if path.startswith("/bg") else Priority.CRITICAL


def build(
    *,
    clock: FakeClock,
    lifo_after: float = 0.050,
    queue_timeout: float = 1.0,
    background_limit: int = 2,
) -> tuple[PriorityShedder, list[str], asyncio.Event]:
    log: list[str] = []
    gate = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        log.append(scope["path"])
        if scope["path"] == "/hold":
            await gate.wait()
        if scope["path"] == "/boom":
            raise RuntimeError("handler crashed")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    shedder = PriorityShedder(
        app,
        classify=classify,
        max_concurrency=1,
        queue_limits={Priority.CRITICAL: 10, Priority.BACKGROUND: background_limit},
        queue_timeout=queue_timeout,
        lifo_after=lifo_after,
        clock=clock,
    )
    return shedder, log, gate


async def settle(predicate: Callable[[], bool]) -> None:
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def settle_waiting(shedder: PriorityShedder, count: int) -> None:
    await settle(lambda: shedder.waiting == count)


def client_for(shedder: PriorityShedder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=shedder), base_url="http://test")


async def queue_up(
    client: httpx.AsyncClient, shedder: PriorityShedder, clock: FakeClock, paths: list[str]
) -> list[asyncio.Task[httpx.Response]]:
    tasks: list[asyncio.Task[httpx.Response]] = []
    for n, path in enumerate(paths):
        clock.now = n * 0.010
        tasks.append(asyncio.create_task(client.get(path)))
        await settle_waiting(shedder, n + 1)
    return tasks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standing_queue_switches_dequeue_order_to_lifo() -> None:
    clock = FakeClock()
    shedder, log, gate = build(clock=clock)
    async with client_for(shedder) as client:
        hold = asyncio.create_task(client.get("/hold"))
        await settle(lambda: shedder.in_flight == 1)
        tasks = await queue_up(client, shedder, clock, ["/a", "/b", "/c"])
        clock.now = 0.200  # oldest waiter is 200 ms old > lifo_after
        gate.set()
        await asyncio.gather(hold, *tasks)
    assert log == ["/hold", "/c", "/b", "/a"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_queue_stays_fifo() -> None:
    clock = FakeClock()
    shedder, log, gate = build(clock=clock)
    async with client_for(shedder) as client:
        hold = asyncio.create_task(client.get("/hold"))
        await settle(lambda: shedder.in_flight == 1)
        tasks = await queue_up(client, shedder, clock, ["/a", "/b", "/c"])
        clock.now = 0.030  # oldest waiter is 30 ms old < lifo_after
        gate.set()
        await asyncio.gather(hold, *tasks)
    assert log == ["/hold", "/a", "/b", "/c"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_critical_lane_is_served_before_older_and_younger_background() -> None:
    clock = FakeClock()
    shedder, log, gate = build(clock=clock, lifo_after=10.0, background_limit=5)
    async with client_for(shedder) as client:
        hold = asyncio.create_task(client.get("/hold"))
        await settle(lambda: shedder.in_flight == 1)
        tasks = await queue_up(client, shedder, clock, ["/bg/old", "/crit", "/bg/new"])
        gate.set()
        await asyncio.gather(hold, *tasks)
    assert log[1] == "/crit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_is_rejected_with_503_and_retry_after_while_critical_queues() -> None:
    clock = FakeClock()
    shedder, log, gate = build(clock=clock)
    async with client_for(shedder) as client:
        hold = asyncio.create_task(client.get("/hold"))
        await settle(lambda: shedder.in_flight == 1)
        tasks = await queue_up(client, shedder, clock, ["/x", "/y"])
        shed = await client.get("/bg/report")
        crit = asyncio.create_task(client.get("/z"))
        await settle(lambda: shedder.waiting == 3)
        gate.set()
        done = await asyncio.gather(hold, crit, *tasks)
    assert shed.status_code == 503
    assert shed.headers["retry-after"] == "1"
    assert "/bg/report" not in log
    assert [r.status_code for r in done] == [200, 200, 200, 200]
    assert shedder.rejected == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_waiters_older_than_queue_timeout_expire_at_dequeue_without_running() -> None:
    clock = FakeClock()
    shedder, log, gate = build(clock=clock, lifo_after=10.0)
    async with client_for(shedder) as client:
        hold = asyncio.create_task(client.get("/hold"))
        await settle(lambda: shedder.in_flight == 1)
        old = await queue_up(client, shedder, clock, ["/old"])
        clock.now = 0.500
        young = asyncio.create_task(client.get("/young"))
        await settle(lambda: shedder.waiting == 2)
        clock.now = 1.200  # /old waited 1.2 s > 1.0 s, /young waited 0.7 s
        gate.set()
        _, old_resp, young_resp = await asyncio.gather(hold, *old, young)
    assert old_resp.status_code == 503
    assert young_resp.status_code == 200
    assert "/old" not in log
    assert shedder.expired == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crashing_handler_releases_its_slot() -> None:
    shedder, _, _ = build(clock=FakeClock(), queue_timeout=0.2)
    async with client_for(shedder) as client:
        with pytest.raises(RuntimeError, match="handler crashed"):
            await client.get("/boom")
        assert shedder.in_flight == 0
        assert (await client.get("/ok")).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_waiter_leaves_the_queue() -> None:
    clock = FakeClock()
    shedder, log, gate = build(clock=clock)
    async with client_for(shedder) as client:
        hold = asyncio.create_task(client.get("/hold"))
        await settle(lambda: shedder.in_flight == 1)
        (gone,) = await queue_up(client, shedder, clock, ["/gone"])
        gone.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gone
        assert shedder.waiting == 0
        gate.set()
        await hold
    assert shedder.in_flight == 0
    assert "/gone" not in log
