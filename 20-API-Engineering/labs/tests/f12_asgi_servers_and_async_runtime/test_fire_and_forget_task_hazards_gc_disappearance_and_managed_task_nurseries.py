"""Lab: unreferenced tasks vanish under GC; a lifespan TaskPool keeps, bounds, and drains them."""

from __future__ import annotations

import asyncio
import gc

import httpx
import pytest
from asgi_lifespan import LifespanManager

from api_labs.f12_asgi_servers_and_async_runtime.fire_and_forget import (
    PoolClosedError,
    ReplyRegistry,
    TaskPool,
    allocations_until_orphan_collected,
    await_reply,
    build_app,
    gather_sibling_orphan,
    orphan_on_registry_future,
    sleeping_orphan_survives_collection,
    stream_reader_orphan,
    unretrieved_exception_messages,
)

DESTROYED = "Task was destroyed but it is pending!"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("eager", [False, True], ids=["lazy", "eager"])
async def test_unreferenced_task_is_destroyed_mid_await(eager: bool) -> None:
    report = await orphan_on_registry_future(keep_reference=False, weak_registry=True, eager=eager)
    assert report.collected
    assert not report.delivered  # the reply arrives after the waiter is gone
    first = ("waiting", "create_task returned") if eager else ("create_task returned", "waiting")
    assert report.events == (*first, "unwound:GeneratorExit")
    assert report.loop_messages == (DESTROYED,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strong_reference_keeps_task_alive_until_reply() -> None:
    report = await orphan_on_registry_future(keep_reference=True, weak_registry=True)
    assert not report.collected
    assert report.delivered
    assert report.events == ("create_task returned", "waiting", "got:pong")
    assert report.loop_messages == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strong_producer_link_also_roots_an_unreferenced_task() -> None:
    report = await orphan_on_registry_future(keep_reference=False, weak_registry=False)
    assert not report.collected
    assert report.events[-1] == "got:pong"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gather_abandons_a_sibling_that_is_then_destroyed_silently() -> None:
    report = await gather_sibling_orphan()
    assert report.collected
    assert not report.delivered
    assert report.events == ("waiting", "gather raised", "unwound:GeneratorExit")
    assert report.loop_messages == ()  # gather disabled the destroyed-pending log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sleeping_orphan_is_rooted_by_the_timer_heap() -> None:
    alive, events = await sleeping_orphan_survives_collection()
    assert alive
    assert events == ("sleeping", "woke")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unretrieved_exception_surfaces_only_at_deallocation() -> None:
    while_referenced, after_release = await unretrieved_exception_messages()
    assert while_referenced == ()
    assert after_release == ("Task exception was never retrieved",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_automatic_gc_destroys_an_orphan_without_explicit_collect() -> None:
    assert gc.isenabled()
    count = await allocations_until_orphan_collected()
    assert count is not None
    assert 0 < count <= 100_000


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("keep_reference", [False, True], ids=["dropped", "kept"])
async def test_stdlib_stream_reader_task_needs_a_strong_reference(keep_reference: bool) -> None:
    report = await stream_reader_orphan(keep_reference=keep_reference)
    if keep_reference:
        assert not report.collected
        assert report.events == ("waiting", "got:pong")
        assert report.loop_messages == ()
    else:
        assert report.collected
        assert report.events == ("waiting", "unwound:GeneratorExit")
        assert report.loop_messages == (DESTROYED,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pool_keeps_unreferenced_jobs_alive_under_gc() -> None:
    registry = ReplyRegistry(weak=True)
    events: list[str] = []
    async with TaskPool(limit=4, drain_timeout=1.0) as pool:
        await pool.spawn(await_reply(registry, "r", events), name="reply")
        await asyncio.sleep(0)
        gc.collect()
        assert registry.deliver("r", "pong")
    assert events == ["waiting", "got:pong"]
    assert pool.stats.completed == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pool_rejects_when_full_and_frees_the_slot_on_completion() -> None:
    gate = asyncio.Event()

    async def blocked() -> None:
        await gate.wait()

    async with TaskPool(limit=2, drain_timeout=1.0) as pool:
        assert await pool.try_spawn(blocked(), name="a") is not None
        assert await pool.try_spawn(blocked(), name="b") is not None
        assert await pool.try_spawn(blocked(), name="c") is None
        assert pool.in_flight == 2
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert pool.in_flight == 0
        assert await pool.try_spawn(blocked(), name="d") is not None
    assert (pool.stats.completed, pool.stats.rejected) == (3, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spawn_waits_for_a_slot_instead_of_rejecting() -> None:
    gate = asyncio.Event()
    order: list[str] = []

    async def job(name: str) -> None:
        await gate.wait()
        order.append(name)

    async with TaskPool(limit=1, drain_timeout=1.0) as pool:
        await pool.spawn(job("first"), name="first")

        async def producer() -> None:
            await pool.spawn(job("second"), name="second")
            order.append("second admitted")

        waiter = asyncio.create_task(producer())
        await asyncio.sleep(0)
        assert not waiter.done()  # blocked on the semaphore: backpressure
        gate.set()
        await waiter
    assert order == ["first", "second admitted", "second"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failing_job_is_recorded_and_does_not_cancel_siblings() -> None:
    finished: list[str] = []

    async def boom() -> None:
        await asyncio.sleep(0)
        raise ConnectionError("smtp refused")

    async def slow() -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        finished.append("slow")

    async with TaskPool(limit=4, drain_timeout=1.0) as pool:
        await pool.spawn(boom(), name="email-1")
        await pool.spawn(slow(), name="report-1")
    assert finished == ["slow"]
    assert (pool.stats.completed, pool.stats.failed, pool.stats.cancelled) == (1, 1, 0)
    assert pool.stats.errors == ["email-1: ConnectionError: smtp refused"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_finishes_fast_jobs_and_cancels_stragglers_at_the_deadline() -> None:
    cleanup: list[str] = []

    async def fast() -> None:
        await asyncio.sleep(0.001)
        cleanup.append("fast done")

    async def stuck() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup.append("stuck cancelled")
            raise

    loop = asyncio.get_running_loop()
    async with TaskPool(limit=4, drain_timeout=0.05) as pool:
        await pool.spawn(fast(), name="fast")
        await pool.spawn(stuck(), name="stuck")
        started = loop.time()
    elapsed = loop.time() - started
    assert cleanup == ["fast done", "stuck cancelled"]
    assert (pool.stats.completed, pool.stats.cancelled) == (1, 1)
    assert 0.05 <= elapsed < 1.0
    assert pool.in_flight == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spawn_after_close_is_refused() -> None:
    async with TaskPool(limit=1, drain_timeout=0.1) as pool:
        pass

    async def job() -> None:
        await asyncio.sleep(0)

    with pytest.raises(PoolClosedError):
        await pool.spawn(job(), name="late")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_pool_returns_202_then_503_and_drains_on_shutdown() -> None:
    gate = asyncio.Event()
    done: list[str] = []

    async def job_factory(job_id: str) -> None:
        await gate.wait()
        done.append(job_id)

    pools: list[TaskPool] = []
    app = build_app(limit=2, drain_timeout=1.0, job_factory=job_factory, pools=pools)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            codes = [(await client.post(f"/jobs/{i}")).status_code for i in range(3)]
            busy = await client.post("/jobs/9")
            assert pools[0].in_flight == 2  # jobs outlive the requests that spawned them
            gate.set()
    assert codes == [202, 202, 503]
    assert busy.headers["retry-after"] == "1"
    assert sorted(done) == ["0", "1"]  # drained before lifespan shutdown completed
    assert (pools[0].stats.completed, pools[0].stats.rejected) == (2, 2)
