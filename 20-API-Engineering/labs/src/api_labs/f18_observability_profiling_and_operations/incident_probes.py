"""Probes behind the triage tree: a real event-loop lag monitor, demo handlers, and a lock queue.

``LoopLagMonitor`` and ``build_demo_app`` run against a real asyncio loop and a real Starlette app.
``simulate_convoy`` is a deterministic model of a PostgreSQL-style FIFO lock queue in front of a
shared connection pool; it is a model of the documented behavior, not a database.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

if TYPE_CHECKING:
    import threading
    from types import TracebackType

    from starlette.requests import Request


class LoopLagMonitor:
    """Samples how late a timer fires compared with its deadline, on the running loop."""

    def __init__(self, interval_s: float = 0.01) -> None:
        self.interval_s = interval_s
        self.samples: list[float] = []
        self._task: asyncio.Task[None] | None = None
        self._deadline: float | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._deadline = loop.time() + self.interval_s
            await asyncio.sleep(self.interval_s)
            self.samples.append(max(0.0, loop.time() - self._deadline))

    async def __aenter__(self) -> LoopLagMonitor:
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._deadline is not None:
            # a stall that ended without ever yielding leaves its timer overdue but unfired
            self.samples.append(max(0.0, asyncio.get_running_loop().time() - self._deadline))

    def max_lag(self) -> float:
        return max(self.samples, default=0.0)

    def percentile(self, q: float) -> float:
        """Nearest-rank percentile of the collected lags (0 when nothing was sampled)."""
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        return ordered[max(0, math.ceil(q / 100.0 * len(ordered)) - 1)]


def burn_cpu(seconds: float) -> None:
    """Spin until this process has consumed *seconds* of CPU time (not wall time)."""
    end = time.process_time() + seconds
    while time.process_time() < end:
        pass


def build_demo_app(block_s: float, gate: threading.Event) -> Starlette:
    """An app whose routes stall in the five different ways the tree must tell apart."""

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def awaits(request: Request) -> PlainTextResponse:
        await asyncio.sleep(block_s)
        return PlainTextResponse("done")

    async def blocking_io(request: Request) -> PlainTextResponse:
        time.sleep(block_s)  # noqa: ASYNC251 - the bug under study: a blocking call on the loop
        return PlainTextResponse("done")

    async def blocking_cpu(request: Request) -> PlainTextResponse:
        burn_cpu(block_s)
        return PlainTextResponse("done")

    def sync_blocking(request: Request) -> PlainTextResponse:
        time.sleep(block_s)  # a plain def runs in the anyio worker-thread pool
        return PlainTextResponse("done")

    def sync_gate(request: Request) -> PlainTextResponse:
        gate.wait(timeout=30.0)  # bounded so a failed test cannot hang the run
        return PlainTextResponse("done")

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/awaits", awaits),
            Route("/blocking-io", blocking_io),
            Route("/blocking-cpu", blocking_cpu),
            Route("/sync-blocking", sync_blocking),
            Route("/sync-gate", sync_gate),
        ]
    )


class LockMode(StrEnum):
    """The three table lock modes the convoy needs (PostgreSQL documentation, Table 13.2)."""

    ACCESS_SHARE = "ACCESS SHARE"
    ROW_EXCLUSIVE = "ROW EXCLUSIVE"
    ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"


def conflicts(a: LockMode, b: LockMode) -> bool:
    """ACCESS EXCLUSIVE conflicts with every mode; the two weaker modes only with it."""
    return LockMode.ACCESS_EXCLUSIVE in (a, b)


@dataclass(slots=True)
class LockRequest:
    pid: int
    mode: LockMode
    hold_s: float
    arrived_s: float
    granted_s: float | None = None


class LockQueue:
    """One lock object: granted holders plus a FIFO of waiters."""

    def __init__(self) -> None:
        self.holders: list[LockRequest] = []
        self.waiters: list[LockRequest] = []

    def request(self, req: LockRequest, now: float) -> bool:
        """Grant at once only if nothing queued ahead or held conflicts (lock.c: waitMask first)."""
        blocked = any(conflicts(req.mode, w.mode) for w in self.waiters) or any(
            conflicts(req.mode, h.mode) for h in self.holders
        )
        if blocked:
            self.waiters.append(req)
            return False
        req.granted_s = now
        self.holders.append(req)
        return True

    def release(self, req: LockRequest, now: float) -> None:
        self.holders.remove(req)
        self._wake(now)

    def cancel(self, req: LockRequest, now: float) -> None:
        """Abort a waiting request (what ``lock_timeout`` does) and let the queue move."""
        self.waiters.remove(req)
        self._wake(now)

    def _wake(self, now: float) -> None:
        still_waiting: list[LockRequest] = []
        for waiter in self.waiters:
            blocked = any(conflicts(waiter.mode, h.mode) for h in self.holders) or any(
                conflicts(waiter.mode, w.mode) for w in still_waiting
            )
            if blocked:
                still_waiting.append(waiter)
            else:
                waiter.granted_s = now
                self.holders.append(waiter)
        self.waiters = still_waiting

    def blocking_pids(self, req: LockRequest) -> tuple[int, ...]:
        """Hard blockers (holders) then soft blockers (earlier waiters), as ``pg_blocking_pids``."""
        hard = [h.pid for h in self.holders if h is not req and conflicts(req.mode, h.mode)]
        soft: list[int] = []
        for waiter in self.waiters:
            if waiter is req:
                break
            if conflicts(req.mode, waiter.mode):
                soft.append(waiter.pid)
        return tuple(hard + soft)


@dataclass(frozen=True, slots=True)
class ConvoyResult:
    pool_exhausted_at_s: float | None
    max_lock_waiters: int
    rejected_requests: int
    rejected_other_table: int
    longest_lock_wait_s: float
    alter_granted_at_s: float | None
    alter_aborted: bool
    recovered_at_s: float | None


def simulate_convoy(
    *,
    alter: bool = True,
    lock_timeout_s: float | None = None,
    pool_size: int = 20,
    rate_rps: float = 100.0,
    table_every: int = 10,
    reader_hold_s: float = 30.0,
    alter_at_s: float = 1.0,
    alter_hold_s: float = 0.05,
    query_s: float = 0.005,
    horizon_s: float = 40.0,
    dt: float = 0.01,
) -> ConvoyResult:
    """A long reader, then an ``ALTER TABLE`` that queues behind it, then ordinary traffic.

    Every *table_every*-th app request reads the locked table; the rest touch other tables.
    All app requests borrow one connection from a shared pool for as long as they run,
    including while they wait for the lock.
    The reader and the ``ALTER`` use dedicated connections outside that pool.
    """
    queue = LockQueue()
    reader = LockRequest(pid=1, mode=LockMode.ACCESS_SHARE, hold_s=reader_hold_s, arrived_s=0.0)
    queue.request(reader, 0.0)
    ddl = LockRequest(2, LockMode.ACCESS_EXCLUSIVE, alter_hold_s, alter_at_s)
    app_requests: list[LockRequest] = []
    other_release_at: list[float] = []
    in_pool = 0
    next_pid = 100
    arrivals = 0
    exhausted_at: float | None = None
    max_waiters = 0
    rejected = 0
    rejected_other = 0
    longest_wait = 0.0
    ddl_granted_at: float | None = None
    ddl_aborted = False
    ddl_submitted = False
    recovered_at: float | None = None
    arrival_credit = 0.0

    for step in range(int(horizon_s / dt) + 1):
        now = step * dt
        # releases
        for held in [h for h in queue.holders if (h.granted_s or 0.0) + h.hold_s <= now]:
            queue.release(held, now)
            if held is ddl:
                ddl_granted_at = ddl.granted_s
            elif held in app_requests:
                app_requests.remove(held)
                in_pool -= 1
        due = [t for t in other_release_at if t <= now]
        in_pool -= len(due)
        other_release_at = [t for t in other_release_at if t > now]
        # the DDL statement arrives, and may time out while it waits
        if alter and not ddl_submitted and now >= alter_at_s:
            ddl_submitted = True
            queue.request(ddl, now)
        if (
            lock_timeout_s is not None
            and ddl in queue.waiters
            and now - ddl.arrived_s >= lock_timeout_s
        ):
            queue.cancel(ddl, now)
            ddl_aborted = True
        # arrivals
        arrival_credit += rate_rps * dt
        while arrival_credit >= 1.0 - 1e-9:
            arrival_credit -= 1.0
            arrivals += 1
            touches_table = arrivals % table_every == 0
            if in_pool >= pool_size:
                rejected += 1
                rejected_other += 0 if touches_table else 1
                if exhausted_at is None:
                    exhausted_at = now
                continue
            in_pool += 1
            if touches_table:
                req = LockRequest(next_pid, LockMode.ACCESS_SHARE, query_s, now)
                next_pid += 1
                app_requests.append(req)
                queue.request(req, now)
            else:
                other_release_at.append(now + query_s)
        for req in app_requests:
            if req.granted_s is not None:
                longest_wait = max(longest_wait, req.granted_s - req.arrived_s)
        max_waiters = max(max_waiters, len(queue.waiters))
        if (
            exhausted_at is not None
            and recovered_at is None
            and not queue.waiters
            and in_pool <= pool_size // 2
        ):
            recovered_at = now

    return ConvoyResult(
        pool_exhausted_at_s=exhausted_at,
        max_lock_waiters=max_waiters,
        rejected_requests=rejected,
        rejected_other_table=rejected_other,
        longest_lock_wait_s=longest_wait,
        alter_granted_at_s=ddl_granted_at,
        alter_aborted=ddl_aborted,
        recovered_at_s=recovered_at,
    )
