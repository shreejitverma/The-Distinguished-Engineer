"""Yield-point races, keyed locks, semaphore caps, and reentrant deadlocks in asyncio.

Every hazard here happens on one thread: a task is only ever preempted at an
`await` that actually suspends, so the race window is the code between a check
and the act that depends on it whenever an `await` sits in between.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from starlette.requests import Request

type IoHook = Callable[[], Awaitable[None]]


async def yield_once() -> None:
    """Stand-in for a database round trip: one real suspension of the task."""
    await asyncio.sleep(0)


class Inventory:
    """A stock counter whose reserve path awaits between the check and the act."""

    def __init__(self, stock: int, io: IoHook = yield_once) -> None:
        self.stock = stock
        self._io = io
        self._lock = asyncio.Lock()

    async def reserve_unsafe(self) -> bool:
        if self.stock <= 0:  # check
            return False
        await self._io()  # yield point: every other task may run here
        self.stock -= 1  # act on a stale check
        return True

    async def reserve_locked(self) -> bool:
        async with self._lock:
            return await self.reserve_unsafe()


def build_app(inventory: Inventory, *, locked: bool) -> Starlette:
    """`POST /reserve` returns `200` on success and `409` when sold out."""

    async def reserve(request: Request) -> JSONResponse:
        ok = await (inventory.reserve_locked() if locked else inventory.reserve_unsafe())
        body = {"reserved": ok, "stock": inventory.stock}
        return JSONResponse(body, status_code=200 if ok else 409)

    return Starlette(routes=[Route("/reserve", reserve, methods=["POST"])])


@dataclass
class _KeyEntry:
    lock: asyncio.Lock
    users: int = 0


class KeyedLocks:
    """One lock per key, deleted when its last holder or waiter leaves."""

    def __init__(self) -> None:
        self._entries: dict[str, _KeyEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncGenerator[None]:
        # No await between lookup and increment, so this is atomic on the loop.
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _KeyEntry(asyncio.Lock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._entries[key]


@dataclass
class ConcurrencyProbe:
    """Records the peak number of callers inside `call` at the same time."""

    in_flight: int = 0
    peak: int = 0

    async def call(self) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            self.in_flight -= 1


async def fan_out(probe: ConcurrencyProbe, jobs: int, limit: asyncio.Semaphore | None) -> None:
    async def one() -> None:
        if limit is None:
            await probe.call()
            return
        async with limit:
            await probe.call()

    async with asyncio.TaskGroup() as tg:
        for _ in range(jobs):
            tg.create_task(one())


class Account:
    """`deposit_reentrant` re-acquires its own lock; `deposit` does not."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.balance = 0
        self.audit: list[int] = []

    async def deposit_reentrant(self, amount: int) -> None:
        async with self._lock:
            self.balance += amount
            await self.record(amount)  # deadlock: record() waits on our own lock

    async def record(self, amount: int) -> None:
        async with self._lock:
            self._record_locked(amount)

    async def deposit(self, amount: int) -> None:
        async with self._lock:
            self.balance += amount
            self._record_locked(amount)

    def _record_locked(self, amount: int) -> None:
        """Caller must hold `self._lock`; never acquires it."""
        if not self._lock.locked():
            raise RuntimeError("_record_locked called without holding the lock")
        self.audit.append(amount)


class TaskRLock:
    """A reentrant lock owned by one asyncio task (asyncio ships no `RLock`)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def acquire(self) -> None:
        me = asyncio.current_task()
        if me is None:
            raise RuntimeError("TaskRLock.acquire() needs a running task")
        if self._owner is me:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = me
        self._depth = 1

    def release(self) -> None:
        if self._owner is None or self._owner is not asyncio.current_task():
            raise RuntimeError("TaskRLock released by a task that does not own it")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()


async def is_stuck(task: asyncio.Task[Any], spins: int = 20) -> bool:
    """Deterministic hang check: run the loop `spins` times, then ask if `task` finished."""
    for _ in range(spins):
        await asyncio.sleep(0)
    return not task.done()


async def transfer(first: asyncio.Lock, second: asyncio.Lock) -> str:
    """Take two locks in the given order; the yield between them opens the ABBA window."""
    async with first:
        await yield_once()
        async with second:
            return "done"


async def transfer_ordered(a: asyncio.Lock, b: asyncio.Lock) -> str:
    """Fix: acquire in one global order (here `id`), whatever order the caller named them."""
    first, second = sorted((a, b), key=id)
    return await transfer(first, second)
