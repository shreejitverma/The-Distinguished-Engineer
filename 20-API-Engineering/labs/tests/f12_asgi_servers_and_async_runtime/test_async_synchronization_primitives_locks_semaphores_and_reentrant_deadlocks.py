"""Lab: Async-Synchronization-Primitives-Locks-Semaphores-and-Reentrant-Deadlocks."""

from __future__ import annotations

import asyncio
import sys

import anyio
import httpx
import pytest

from api_labs.f12_asgi_servers_and_async_runtime.async_sync_primitives import (
    Account,
    ConcurrencyProbe,
    Inventory,
    KeyedLocks,
    TaskRLock,
    build_app,
    fan_out,
    is_stuck,
    transfer,
    transfer_ordered,
)


async def _five_reservations(locked: bool) -> tuple[list[int], int]:
    inventory = Inventory(stock=1)
    transport = httpx.ASGITransport(app=build_app(inventory, locked=locked))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.post("/reserve") for _ in range(5)))
    return sorted(r.status_code for r in responses), inventory.stock


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_then_act_across_await_oversells_one_unit_five_times() -> None:
    statuses, stock = await _five_reservations(locked=False)
    assert statuses == [200, 200, 200, 200, 200]
    assert stock == -4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lock_around_check_and_act_sells_exactly_one() -> None:
    statuses, stock = await _five_reservations(locked=True)
    assert statuses == [200, 409, 409, 409, 409]
    assert stock == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_await_between_check_and_act_there_is_no_race() -> None:
    async def no_io() -> None:
        return None

    inventory = Inventory(stock=1, io=no_io)
    results = await asyncio.gather(*(inventory.reserve_unsafe() for _ in range(5)))
    assert sorted(results) == [False, False, False, False, True]
    assert inventory.stock == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keyed_locks_serialize_one_key_and_overlap_different_keys() -> None:
    keyed = KeyedLocks()
    same, different = ConcurrencyProbe(), ConcurrencyProbe()

    async def work(key: str, probe: ConcurrencyProbe) -> None:
        async with keyed.hold(key):
            await probe.call()

    await asyncio.gather(*(work("order-1", same) for _ in range(3)))
    await asyncio.gather(*(work(f"order-{i}", different) for i in range(3)))
    assert same.peak == 1
    assert different.peak == 3
    assert len(keyed) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keyed_locks_drop_entry_when_queued_waiter_is_cancelled() -> None:
    keyed = KeyedLocks()
    release = asyncio.Event()

    async def holder() -> None:
        async with keyed.hold("k"):
            await release.wait()

    async def waiter() -> None:
        async with keyed.hold("k"):
            pass

    h = asyncio.create_task(holder())
    w = asyncio.create_task(waiter())
    assert await is_stuck(w)
    w.cancel()
    await asyncio.gather(w, return_exceptions=True)
    release.set()
    await h
    assert w.cancelled()
    assert len(keyed) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_semaphore_caps_peak_concurrency() -> None:
    unbounded, capped = ConcurrencyProbe(), ConcurrencyProbe()
    await fan_out(unbounded, jobs=20, limit=None)
    await fan_out(capped, jobs=20, limit=asyncio.Semaphore(4))
    assert unbounded.peak == 20
    assert capped.peak == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extra_release_silently_raises_semaphore_cap() -> None:
    sem = asyncio.Semaphore(4)
    sem.release()  # unbalanced: no matching acquire
    probe = ConcurrencyProbe()
    await fan_out(probe, jobs=20, limit=sem)
    assert probe.peak == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounded_semaphore_rejects_extra_release() -> None:
    sem = asyncio.BoundedSemaphore(4)
    with pytest.raises(ValueError, match="released too many times"):
        sem.release()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reacquiring_asyncio_lock_in_same_task_hangs_forever() -> None:
    account = Account()
    task = asyncio.create_task(account.deposit_reentrant(10))
    assert await is_stuck(task, spins=100)
    assert account.balance == 10
    assert account.audit == []
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not account._lock.locked()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lock_held_helper_split_completes() -> None:
    account = Account()
    await asyncio.wait_for(account.deposit(10), timeout=1)
    assert account.balance == 10
    assert account.audit == [10]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anyio_lock_raises_on_reentry_instead_of_hanging() -> None:
    lock = anyio.Lock()
    async with lock:
        with pytest.raises(RuntimeError, match="already held"):
            await lock.acquire()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_rlock_reenters_in_owner_and_blocks_other_tasks() -> None:
    rlock = TaskRLock()
    order: list[str] = []

    async def other() -> None:
        async with rlock:
            order.append("other")

    async with rlock:
        async with rlock:
            t = asyncio.create_task(other())
            assert await is_stuck(t)
            order.append("owner-inner")
        assert await is_stuck(t)
        order.append("owner-outer")
    await t
    assert order == ["owner-inner", "owner-outer", "other"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_rlock_reentrancy_does_not_cross_into_child_task() -> None:
    rlock = TaskRLock()

    async def parent() -> None:
        async with rlock:
            await asyncio.create_task(rlock.acquire())  # child is a different owner

    task = asyncio.create_task(parent())
    assert await is_stuck(task, spins=100)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_rlock_refuses_release_by_non_owner() -> None:
    rlock = TaskRLock()
    await rlock.acquire()

    async def intruder() -> None:
        rlock.release()

    with pytest.raises(RuntimeError, match="does not own"):
        await asyncio.create_task(intruder())
    rlock.release()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncio_lock_has_no_owner_so_any_task_can_release_it() -> None:
    lock = asyncio.Lock()
    await lock.acquire()

    async def intruder() -> None:
        lock.release()

    await asyncio.create_task(intruder())
    assert not lock.locked()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lock_is_fifo_and_releaser_cannot_barge() -> None:
    lock = asyncio.Lock()
    order: list[str] = []

    async def b() -> None:
        async with lock:
            order.append("b")

    await lock.acquire()
    t = asyncio.create_task(b())
    assert await is_stuck(t)
    lock.release()
    await lock.acquire()  # b's waiter future is done but not cancelled: no fast path
    order.append("a")
    lock.release()
    await t
    assert order == ["b", "a"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_woken_waiter_passes_lock_to_next_waiter() -> None:
    lock = asyncio.Lock()
    order: list[str] = []

    async def waiter(name: str) -> None:
        async with lock:
            order.append(name)

    await lock.acquire()
    b = asyncio.create_task(waiter("b"))
    c = asyncio.create_task(waiter("c"))
    assert await is_stuck(c)
    lock.release()  # sets b's future
    b.cancel()  # b is cancelled before it runs and takes the lock
    await asyncio.gather(b, c, return_exceptions=True)
    assert b.cancelled()
    assert order == ["c"]
    assert not lock.locked()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_condition_notify_lost_when_woken_waiter_is_cancelled_before_3_13() -> None:
    cond = asyncio.Condition()
    woke: list[str] = []

    async def waiter(name: str) -> None:
        async with cond:
            await cond.wait()
            woke.append(name)

    w1 = asyncio.create_task(waiter("w1"))
    w2 = asyncio.create_task(waiter("w2"))
    assert await is_stuck(w2)
    async with cond:
        cond.notify(1)  # wakes w1 only
        w1.cancel()  # w1 dies before it consumes the notification
    stuck = await is_stuck(w2)
    if sys.version_info >= (3, 13):
        assert not stuck  # gh-112202: the failing waiter re-notifies the next one
        assert woke == ["w2"]
    else:
        assert stuck  # the notification is lost
        assert woke == []
        w2.cancel()
    await asyncio.gather(w1, w2, return_exceptions=True)
    assert w1.cancelled()


async def _contend(lock: asyncio.Lock) -> None:
    async def other() -> None:
        async with lock:
            pass

    async with lock:
        t = asyncio.create_task(other())
        await asyncio.sleep(0)  # other() queues a waiter future: binds the lock to this loop
    await t


async def _uncontended(lock: asyncio.Lock) -> None:
    async with lock:
        pass


@pytest.mark.unit
def test_lock_binds_to_first_loop_only_when_contended() -> None:
    lock = asyncio.Lock()
    asyncio.run(_uncontended(lock))
    asyncio.run(_uncontended(lock))  # fast path never calls _get_loop(): no binding
    asyncio.run(_contend(lock))
    with pytest.raises(RuntimeError, match="bound to a different event loop"):
        asyncio.run(_contend(lock))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_opposite_lock_order_deadlocks_and_one_global_order_does_not() -> None:
    a, b = asyncio.Lock(), asyncio.Lock()
    t1 = asyncio.create_task(transfer(a, b))
    t2 = asyncio.create_task(transfer(b, a))
    assert await is_stuck(t1)
    assert await is_stuck(t2)
    assert a.locked()
    assert b.locked()
    for t in (t1, t2):
        t.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
    assert not a.locked()
    assert not b.locked()

    ordered = await asyncio.gather(transfer_ordered(a, b), transfer_ordered(b, a))
    assert list(ordered) == ["done", "done"]
