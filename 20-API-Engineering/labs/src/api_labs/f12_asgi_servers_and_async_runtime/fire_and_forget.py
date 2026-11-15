"""Fire-and-forget task hazards and a lifespan-scoped task pool.

The probes show when the cycle collector destroys a pending ``asyncio.Task``:
only when no strong path leads from a loop root (the ready queue, the timer
heap, the selector map) or from user code to the future the task awaits.
``TaskPool`` is the fix: a lifespan-owned ``asyncio.TaskGroup`` that holds
strong references, bounds admission, captures job errors, and drains on exit.
"""

from __future__ import annotations

import asyncio
import gc
import socket
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from types import TracebackType

    from starlette.requests import Request

Job = Coroutine[Any, Any, None]


def _capture_loop_messages(loop: asyncio.AbstractEventLoop, sink: list[str]) -> None:
    def handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        sink.append(str(context["message"]))

    loop.set_exception_handler(handler)


class ReplyRegistry:
    """Correlation-id -> future map that holds futures weakly.

    The weak values look like leak hygiene (a caller that gave up does not pin
    its future), but they also remove the only strong path from the producer
    to the waiting task: the same shape as ``StreamReaderProtocol``, which
    holds its ``StreamReader`` through ``weakref.ref`` (gh-90467).
    """

    def __init__(self, *, weak: bool) -> None:
        self._pending: (
            weakref.WeakValueDictionary[str, asyncio.Future[str]] | dict[str, asyncio.Future[str]]
        ) = weakref.WeakValueDictionary() if weak else {}

    def expect(self, key: str) -> asyncio.Future[str]:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        return future

    def deliver(self, key: str, payload: str) -> bool:
        future = self._pending.pop(key, None)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True


async def await_reply(registry: ReplyRegistry, key: str, events: list[str]) -> None:
    """Background job: wait for a reply, record how the frame was unwound."""
    future = registry.expect(key)
    events.append("waiting")
    try:
        payload = await future
    except BaseException as exc:  # GeneratorExit when the collector closes the coroutine
        events.append(f"unwound:{type(exc).__name__}")
        raise
    events.append(f"got:{payload}")


@dataclass(frozen=True)
class OrphanReport:
    collected: bool
    delivered: bool
    events: tuple[str, ...]
    loop_messages: tuple[str, ...]


async def orphan_on_registry_future(
    *, keep_reference: bool, weak_registry: bool, eager: bool = False
) -> OrphanReport:
    """Spawn ``await_reply`` with or without a strong task reference, then force a collection.

    With ``eager=True`` the loop uses ``asyncio.eager_task_factory`` (3.12+), so
    the first step runs inside ``create_task`` before it returns.
    """
    loop = asyncio.get_running_loop()
    messages: list[str] = []
    previous = loop.get_exception_handler()
    previous_factory = loop.get_task_factory()
    _capture_loop_messages(loop, messages)
    try:
        registry = ReplyRegistry(weak=weak_registry)
        events: list[str] = []
        background: set[asyncio.Task[None]] = set()
        if eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        task = asyncio.create_task(await_reply(registry, "req-1", events))
        loop.set_task_factory(previous_factory)
        events.append("create_task returned")
        if keep_reference:
            background.add(task)
            task.add_done_callback(background.discard)
        probe = weakref.ref(task)
        del task
        await asyncio.sleep(0)  # first step: the task parks on its future
        gc.collect()
        collected = probe() is None
        delivered = registry.deliver("req-1", "pong")
        await asyncio.sleep(0)  # let a surviving task consume the reply
        return OrphanReport(collected, delivered, tuple(events), tuple(messages))
    finally:
        loop.set_task_factory(previous_factory)
        loop.set_exception_handler(previous)


async def gather_sibling_orphan() -> OrphanReport:
    """``gather`` raises on the first failure and abandons the other child.

    ``gather`` sets ``_log_destroy_pending = False`` on tasks it creates from
    bare coroutines, so when the collector destroys the abandoned sibling no
    "Task was destroyed" message is logged: the loss is silent.
    """
    loop = asyncio.get_running_loop()
    messages: list[str] = []
    previous = loop.get_exception_handler()
    _capture_loop_messages(loop, messages)
    try:
        registry = ReplyRegistry(weak=True)
        events: list[str] = []

        async def fail() -> None:
            await asyncio.sleep(0)
            raise ValueError("validation failed")

        try:
            await asyncio.gather(await_reply(registry, "g", events), fail())
        except ValueError:
            events.append("gather raised")
        # The loop's Handle that delivered the failure to this task holds the
        # _GatheringFuture (and through _children the sibling) until we yield.
        await asyncio.sleep(0)
        gc.collect()
        delivered = registry.deliver("g", "pong")
        await asyncio.sleep(0)
        collected = "unwound:GeneratorExit" in events
        return OrphanReport(collected, delivered, tuple(events), tuple(messages))
    finally:
        loop.set_exception_handler(previous)


async def sleeping_orphan_survives_collection() -> tuple[bool, tuple[str, ...]]:
    """An unreferenced task in ``asyncio.sleep`` is rooted by the loop's timer heap."""
    events: list[str] = []

    async def nap() -> None:
        events.append("sleeping")
        await asyncio.sleep(0.01)
        events.append("woke")

    probe = weakref.ref(asyncio.create_task(nap()))  # dangling on purpose
    await asyncio.sleep(0)
    gc.collect()
    alive = probe() is not None
    await asyncio.sleep(0.05)
    return alive, tuple(events)


async def unretrieved_exception_messages() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return loop messages seen (while a failed task is referenced, after it is released)."""
    loop = asyncio.get_running_loop()
    messages: list[str] = []
    previous = loop.get_exception_handler()
    _capture_loop_messages(loop, messages)
    try:

        async def send_email() -> None:
            await asyncio.sleep(0)
            raise ConnectionError("smtp refused")

        task = asyncio.create_task(send_email())
        await asyncio.wait({task})
        while_referenced = tuple(messages)
        del task
        gc.collect()
        return while_referenced, tuple(messages)
    finally:
        loop.set_exception_handler(previous)


async def allocations_until_orphan_collected(limit: int = 1_000_000) -> int | None:
    """Count container allocations until the automatic collector destroys an orphan.

    No explicit ``gc.collect()`` runs after the orphan is parked, so the count
    is how long a worker keeps a dangling task before it silently vanishes.
    Death is observed through a weakref callback, never by calling the weakref:
    since 3.12 an allocation-triggered collection runs at the next eval-breaker
    check, and a ``probe()`` result sitting on the value stack at that instant
    pins the task (measured; see the note).  Returns ``None`` if the task
    survives ``limit`` allocations.
    """
    loop = asyncio.get_running_loop()
    messages: list[str] = []
    previous = loop.get_exception_handler()
    _capture_loop_messages(loop, messages)
    try:
        registry = ReplyRegistry(weak=True)
        junk: list[list[int]] = []
        died_at: list[int] = []
        gc.collect()  # start from an empty young generation
        task = asyncio.create_task(await_reply(registry, "k", []))
        probe = weakref.ref(task, lambda _ref: died_at.append(len(junk)))
        del task
        await asyncio.sleep(0)  # park the orphan on its weakly held future
        while len(junk) < limit and not died_at:
            junk.append([])
        del probe
        return died_at[0] if died_at else None
    finally:
        loop.set_exception_handler(previous)


async def stream_reader_orphan(*, keep_reference: bool) -> OrphanReport:
    """The stdlib case from gh-91887: a task blocked in ``StreamReader.read``.

    The selector holds the transport and the protocol, but the protocol holds
    the reader only weakly, so nothing strong reaches the reader's waiter
    future or the task parked on it.  Uses ``socket.socketpair()``, no network.
    """
    loop = asyncio.get_running_loop()
    messages: list[str] = []
    previous = loop.get_exception_handler()
    _capture_loop_messages(loop, messages)
    ours, theirs = socket.socketpair()
    events: list[str] = []

    async def consume() -> None:
        reader, writer = await asyncio.open_connection(sock=ours)
        events.append("waiting")
        try:
            data = await reader.read(100)
        except BaseException as exc:
            events.append(f"unwound:{type(exc).__name__}")
            raise
        events.append(f"got:{data.decode()}")
        writer.close()

    background: set[asyncio.Task[None]] = set()
    try:
        task = asyncio.create_task(consume())
        if keep_reference:
            background.add(task)
            task.add_done_callback(background.discard)
        probe = weakref.ref(task)
        del task
        for _ in range(5):  # connect, then park in read()
            await asyncio.sleep(0)
        gc.collect()
        collected = probe() is None
        theirs.sendall(b"pong")
        for _ in range(10):
            await asyncio.sleep(0.005)
        return OrphanReport(collected, not collected, tuple(events), tuple(messages))
    finally:
        theirs.close()
        for _ in range(3):
            await asyncio.sleep(0)
        ours.close()
        loop.set_exception_handler(previous)


class PoolClosedError(RuntimeError):
    """Raised by spawn calls after the pool started draining."""


@dataclass
class PoolStats:
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list[str])


class TaskPool:
    """A lifespan-scoped nursery for background jobs.

    - Strong references: every job is a child of one ``asyncio.TaskGroup``.
    - Backpressure: at most ``limit`` jobs admitted (queued or running).
    - Isolation: a job's ``Exception`` is recorded, not propagated, so one bad
      job never aborts the group and cancels its siblings.
    - Drain: on exit, refuse new jobs, wait up to ``drain_timeout`` seconds,
      cancel stragglers, and let the group await their cleanup.

    ``__aenter__`` and ``__aexit__`` must run in the same task (the lifespan
    task); spawning may happen from any task on the same loop.
    """

    def __init__(self, *, limit: int, drain_timeout: float) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._drain_timeout = drain_timeout
        self._slots = asyncio.Semaphore(limit)
        self._group = asyncio.TaskGroup()
        self._tasks: set[asyncio.Task[bool]] = set()
        self._closing = False
        self.stats = PoolStats()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def __aenter__(self) -> Self:
        await self._group.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._closing = True
        try:
            if exc is None and self._tasks:
                _, late = await asyncio.wait(set(self._tasks), timeout=self._drain_timeout)
                for task in late:
                    task.cancel(msg="drain deadline exceeded")
        except BaseException as drain_exc:  # cancelled while draining: cancel all children
            await self._group.__aexit__(type(drain_exc), drain_exc, drain_exc.__traceback__)
            raise
        await self._group.__aexit__(exc_type, exc, tb)

    async def try_spawn(self, job: Job, *, name: str) -> asyncio.Task[bool] | None:
        """Admit ``job`` if a slot is free right now; otherwise close it and return None."""
        if self._closing:
            job.close()
            raise PoolClosedError("pool is draining")
        if self._slots.locked():
            job.close()
            self.stats.rejected += 1
            return None
        return await self._admit(job, name)

    async def spawn(self, job: Job, *, name: str) -> asyncio.Task[bool]:
        """Admit ``job``, waiting for a slot (caller-side backpressure)."""
        if self._closing:
            job.close()
            raise PoolClosedError("pool is draining")
        return await self._admit(job, name)

    async def _admit(self, job: Job, name: str) -> asyncio.Task[bool]:
        try:
            await self._slots.acquire()  # does not suspend when a slot is free
        except BaseException:
            job.close()
            raise
        if self._closing:  # closed while we waited for a slot
            self._slots.release()
            job.close()
            raise PoolClosedError("pool is draining")
        task = self._group.create_task(self._run(job, name), name=name)
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._on_done(done, job))
        return task

    async def _run(self, job: Job, name: str) -> bool:
        try:
            await job
        except Exception as exc:  # isolate: record, do not abort the group
            self.stats.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return False
        return True

    def _on_done(self, task: asyncio.Task[bool], job: Job) -> None:
        self._tasks.discard(task)
        self._slots.release()
        job.close()  # no-op if it ran; silences "never awaited" if cancelled before start
        if task.cancelled():
            self.stats.cancelled += 1
        elif task.exception() is None:
            if task.result():
                self.stats.completed += 1
            else:
                self.stats.failed += 1


def build_app(
    *,
    limit: int,
    drain_timeout: float,
    job_factory: Callable[[str], Awaitable[None]],
    pools: list[TaskPool],
) -> Starlette:
    """A Starlette app whose lifespan owns a ``TaskPool``; ``POST /jobs/{id}`` enqueues."""

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[dict[str, TaskPool]]:
        async with TaskPool(limit=limit, drain_timeout=drain_timeout) as pool:
            pools.append(pool)
            yield {"pool": pool}

    async def enqueue(request: Request) -> JSONResponse:
        pool: TaskPool = request.state.pool
        job_id = str(request.path_params["job_id"])

        async def job() -> None:
            await job_factory(job_id)

        try:
            task = await pool.try_spawn(job(), name=f"job-{job_id}")
        except PoolClosedError:
            task = None
        if task is None:
            return JSONResponse({"error": "busy"}, status_code=503, headers={"Retry-After": "1"})
        return JSONResponse({"accepted": job_id}, status_code=202)

    return Starlette(routes=[Route("/jobs/{job_id}", enqueue, methods=["POST"])], lifespan=lifespan)
