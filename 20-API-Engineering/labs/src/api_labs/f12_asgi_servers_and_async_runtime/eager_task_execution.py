"""Execution tracer and ASGI cache gateway for asyncio.eager_task_factory.

Demonstrates synchronous first-step coroutine execution, throughput optimization,
execution order shifts, lock interleaving, and stack depth behaviors under
CPython 3.12-3.15.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send


request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id_var", default="anonymous"
)


@dataclass
class TraceRecord:
    """Recorded trace event during task scheduling and execution."""

    event: str
    task_name: str
    is_eager: bool
    is_done: bool


class ExecutionTracer:
    """Traces interleaving order between caller execution and coroutine bodies."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)

    async def run_synchronous_coro(self, name: str) -> str:
        self.record(f"{name}:start")
        self.record(f"{name}:end")
        return f"{name}:result"

    async def run_yielding_coro(self, name: str) -> str:
        self.record(f"{name}:start")
        await asyncio.sleep(0)
        self.record(f"{name}:end")
        return f"{name}:result"

    async def trace_lazy_vs_eager(self, *, use_eager: bool, yield_in_coro: bool) -> list[str]:
        self.events.clear()
        loop = asyncio.get_running_loop()
        old_factory = loop.get_task_factory()
        if use_eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        else:
            loop.set_task_factory(None)

        try:
            self.record("caller:before_create")
            coro = (
                self.run_yielding_coro("task1")
                if yield_in_coro
                else self.run_synchronous_coro("task1")
            )
            task = asyncio.create_task(coro)
            self.record(f"caller:after_create:done={task.done()}")
            self.record("caller:before_await")
            await task
            self.record("caller:after_await")
            return list(self.events)
        finally:
            loop.set_task_factory(old_factory)


class EagerCacheGateway:
    """In-memory cached ASGI gateway evaluating synchronous vs asynchronous response paths."""

    def __init__(self) -> None:
        self.cache: dict[str, bytes] = {}
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.created_tasks: list[asyncio.Task[Any]] = []

    def set_cache(self, path: str, payload: bytes) -> None:
        self.cache[path] = payload

    async def fetch_upstream(self, path: str) -> bytes:
        # Simulate asynchronous I/O (e.g. database query or network roundtrip)
        await asyncio.sleep(0.001)
        data = f"upstream_data_for_{path}".encode()
        self.cache[path] = data
        return data

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        if path in self.cache:
            self.cache_hits += 1
            body = self.cache[path]
            status = 200
        else:
            self.cache_misses += 1
            body = await self.fetch_upstream(path)
            status = 200

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (
                        b"x-cache",
                        b"HIT" if self.cache_hits > self.cache_misses else b"MISS",
                    ),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )

    async def handle_request_with_probe(
        self, scope: Scope, receive: Receive, send: Send, *, use_eager: bool
    ) -> tuple[asyncio.Task[None], bool]:
        """Dispatches request via create_task, recording whether it completed eagerly."""
        loop = asyncio.get_running_loop()
        old_factory = loop.get_task_factory()
        if use_eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        else:
            loop.set_task_factory(None)

        try:
            task: asyncio.Task[None] = asyncio.create_task(self(scope, receive, send))
            was_eager_done = task.done()
            self.created_tasks.append(task)
            await task
            return task, was_eager_done
        finally:
            loop.set_task_factory(old_factory)


def measure_create_task_await_ns(
    iterations: int, *, use_eager: bool, yield_in_coro: bool, repeats: int = 5
) -> float:
    """Return the best-of-``repeats`` mean nanoseconds for one ``create_task`` plus ``await``.

    Each repeat runs on a fresh event loop so that loop state does not leak between runs.
    """

    async def sync_body() -> int:
        return 1

    async def yielding_body() -> int:
        await asyncio.sleep(0)
        return 1

    body = yielding_body if yield_in_coro else sync_body

    async def driver() -> int:
        loop = asyncio.get_running_loop()
        loop.set_task_factory(asyncio.eager_task_factory if use_eager else None)
        start = time.perf_counter_ns()
        for _ in range(iterations):
            await asyncio.create_task(body())
        return time.perf_counter_ns() - start

    return min(asyncio.run(driver()) for _ in range(repeats)) / iterations


def _empty_str_list() -> list[str]:
    return []


@dataclass
class LockProbeResult:
    events: list[str] = field(default_factory=_empty_str_list)
    t1_done_immediate: bool = False
    t2_done_immediate: bool = False


class LockInterleavingProbe:
    """Demonstrates lock acquisition behavior under eager vs lazy task factories."""

    @staticmethod
    async def run_unlocked_probe(*, use_eager: bool) -> LockProbeResult:
        loop = asyncio.get_running_loop()
        old_factory = loop.get_task_factory()
        if use_eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        else:
            loop.set_task_factory(None)

        result = LockProbeResult()
        lock = asyncio.Lock()

        async def worker(name: str) -> None:
            result.events.append(f"{name}:acquire_attempt")
            async with lock:
                result.events.append(f"{name}:in_lock")
            result.events.append(f"{name}:released")

        try:
            result.events.append("main:create_t1")
            t1 = asyncio.create_task(worker("t1"))
            result.t1_done_immediate = t1.done()
            result.events.append("main:create_t2")
            t2 = asyncio.create_task(worker("t2"))
            result.t2_done_immediate = t2.done()
            result.events.append("main:await_tasks")
            await asyncio.gather(t1, t2)
            result.events.append("main:done")
            return result
        finally:
            loop.set_task_factory(old_factory)


class CancellationProbe:
    """Evaluates the behavior of task.cancel() on eager vs lazy tasks."""

    @staticmethod
    async def cancel_synchronous_task(*, use_eager: bool) -> tuple[bool, bool, str]:
        """Returns (cancel_return_code, is_cancelled, task_result_or_exc)."""
        loop = asyncio.get_running_loop()
        old_factory = loop.get_task_factory()
        if use_eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        else:
            loop.set_task_factory(None)

        async def fast_work() -> str:
            return "completed_work"

        try:
            task = asyncio.create_task(fast_work())
            cancel_success = task.cancel()
            is_cancelled = task.cancelled()
            try:
                res = await task
                return cancel_success, is_cancelled, str(res)
            except asyncio.CancelledError:
                return cancel_success, is_cancelled, "caught_cancelled_error"
        finally:
            loop.set_task_factory(old_factory)


class RecursionDepthProbe:
    """Evaluates recursion depth limits under synchronous eager task creation."""

    @staticmethod
    async def spawn_recursive_chain(depth: int, *, use_eager: bool) -> int:
        loop = asyncio.get_running_loop()
        old_factory = loop.get_task_factory()
        if use_eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        else:
            loop.set_task_factory(None)

        async def recurse(n: int) -> int:
            if n <= 0:
                return 0
            sub_coro = recurse(n - 1)
            try:
                sub_task = asyncio.create_task(sub_coro)
            except BaseException:
                sub_coro.close()
                raise
            return 1 + await sub_task

        root_coro = recurse(depth)
        try:
            root_task = asyncio.create_task(root_coro)
            return await root_task
        except BaseException:
            root_coro.close()
            raise
        finally:
            loop.set_task_factory(old_factory)


class ContextVarIsolationProbe:
    """Verifies ContextVar isolation during synchronous first-step execution."""

    @staticmethod
    async def run_context_probe(*, use_eager: bool) -> tuple[str, str]:
        loop = asyncio.get_running_loop()
        old_factory = loop.get_task_factory()
        if use_eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        else:
            loop.set_task_factory(None)

        request_id_var.set("parent_context_tag")

        async def child_task() -> str:
            request_id_var.set("child_mutated_tag")
            return request_id_var.get()

        try:
            task = asyncio.create_task(child_task())
            parent_seen_val = request_id_var.get()
            child_val = await task
            return parent_seen_val, child_val
        finally:
            loop.set_task_factory(old_factory)
