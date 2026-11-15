"""Where FastAPI runs async def and def code, and how AnyIO's CapacityLimiter gates threads."""

import threading
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated

import anyio
import anyio.from_thread
import anyio.lowlevel
import anyio.to_thread
from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, field_validator

REQUEST_TAG: ContextVar[str] = ContextVar("request_tag", default="unset")


@dataclass
class ThreadProbe:
    """Thread-safe record of which OS thread ran each piece of request code."""

    gate: threading.Event = field(default_factory=threading.Event)
    gate_timeout: float = 5.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    in_flight: int = 0
    peak_in_flight: int = 0
    entered: int = 0
    worker_idents: set[int] = field(default_factory=set[int])
    where: dict[str, int] = field(default_factory=dict[str, int])
    events: list[str] = field(default_factory=list[str])

    def record(self, event: str) -> None:
        with self.lock:
            self.events.append(event)

    def mark(self, label: str) -> None:
        with self.lock:
            self.where[label] = threading.get_ident()

    def blocking_work(self) -> bool:
        """Occupy the calling thread until the test opens the gate (or the timeout fires)."""
        with self.lock:
            self.in_flight += 1
            self.entered += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.worker_idents.add(threading.get_ident())
        try:
            return self.gate.wait(self.gate_timeout)
        finally:
            with self.lock:
                self.in_flight -= 1


@dataclass
class ThreadpoolLab:
    app: FastAPI
    bulkhead: anyio.CapacityLimiter


def build_threadpool_app(
    probe: ThreadProbe,
    *,
    thread_tokens: int | None = None,
    bulkhead_tokens: int = 2,
) -> ThreadpoolLab:
    """Build an app whose routes expose where each callable runs and what the limiter holds."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # The default limiter is a RunVar: it exists per event loop, so size it inside the loop.
        if thread_tokens is not None:
            anyio.to_thread.current_default_thread_limiter().total_tokens = thread_tokens
        yield

    app = FastAPI(lifespan=lifespan)
    # Created outside any loop: anyio returns a CapacityLimiterAdapter that binds lazily.
    bulkhead = anyio.CapacityLimiter(bulkhead_tokens)

    class Echo(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def note_thread(cls, value: str) -> str:
            probe.mark(f"response-validation:{value}")
            return value

    def sync_dep() -> str:
        probe.mark("sync-dep")
        return "sync"

    async def async_dep() -> str:
        probe.mark("async-dep")
        return "async"

    def sync_tagging_dep() -> None:
        REQUEST_TAG.set("set-in-sync-dep")

    async def async_tagging_dep() -> None:
        REQUEST_TAG.set("set-in-async-dep")

    @app.get("/blocking", response_model=None)
    def blocking() -> dict[str, bool]:
        probe.record("blocking:enter")
        released = probe.blocking_work()
        probe.record("blocking:exit")
        return {"released": released}

    @app.get("/async-blocker", response_model=None)
    async def async_blocker(hold: float) -> dict[str, bool]:
        probe.record("async-blocker:enter")
        released = probe.gate.wait(hold)  # Blocks the event loop thread: the bug under test.
        probe.record(f"async-blocker:exit(released={released})")
        return {"released": released}

    @app.get("/report", response_model=None)
    async def report() -> dict[str, bool]:
        released = await anyio.to_thread.run_sync(probe.blocking_work, limiter=bulkhead)
        return {"released": released}

    @app.get("/loop-ping", response_model=None)
    async def loop_ping() -> dict[str, float]:
        probe.record("loop-ping")
        stats = anyio.to_thread.current_default_thread_limiter().statistics()
        return {
            "ident": threading.get_ident(),
            "borrowed": stats.borrowed_tokens,
            "total": stats.total_tokens,
            "waiting": stats.tasks_waiting,
        }

    @app.get("/sync-ping", response_model=None)
    def sync_ping() -> dict[str, int]:
        probe.record("sync-ping")
        return {"ident": threading.get_ident()}

    @app.get("/where/sync", response_model=Echo)
    def where_sync(dep: Annotated[str, Depends(sync_dep)]) -> dict[str, str]:
        probe.mark("sync-endpoint")
        return {"value": "sync-endpoint"}

    @app.get("/where/async", response_model=Echo)
    async def where_async(dep: Annotated[str, Depends(async_dep)]) -> dict[str, str]:
        probe.mark("async-endpoint")
        return {"value": "async-endpoint"}

    @app.get("/where/mixed", response_model=Echo)
    async def where_mixed(dep: Annotated[str, Depends(sync_dep)]) -> dict[str, str]:
        probe.mark("mixed-endpoint")
        return {"value": "mixed-endpoint"}

    @app.get("/ctx/into-thread", dependencies=[Depends(async_tagging_dep)])
    def ctx_into_thread() -> dict[str, str]:
        return {"tag": REQUEST_TAG.get()}

    @app.get("/ctx/out-of-thread", dependencies=[Depends(sync_tagging_dep)])
    async def ctx_out_of_thread() -> dict[str, str]:
        return {"tag": REQUEST_TAG.get()}

    return ThreadpoolLab(app=app, bulkhead=bulkhead)


async def cancel_while_in_thread(*, abandon_on_cancel: bool) -> list[str]:
    """Cancel a task while its run_sync call is executing; log what the caller observes."""
    log: list[str] = []
    entered = anyio.Event()
    release = threading.Event()
    exited = threading.Event()
    limiter = anyio.CapacityLimiter(1)

    def work() -> str:
        anyio.from_thread.run_sync(entered.set)
        release.wait(5.0)
        log.append("thread:exit")
        exited.set()
        return "done"

    async with anyio.create_task_group() as tg:
        scope = anyio.CancelScope()

        async def caller() -> None:
            with scope:
                result = await anyio.to_thread.run_sync(
                    work, abandon_on_cancel=abandon_on_cancel, limiter=limiter
                )
                log.append(f"caller:returned({result})")
                await anyio.lowlevel.checkpoint()  # Delivers the pending cancellation.
                log.append("caller:unreachable")
            log.append(f"caller:after-scope(cancelled_caught={scope.cancelled_caught})")

        async def canceller() -> None:
            await entered.wait()
            scope.cancel()
            log.append("cancel:requested")
            await anyio.sleep(0.05)
            log.append("thread:released")
            release.set()

        tg.start_soon(caller)
        tg.start_soon(canceller)

    # An abandoned worker may still be running after the task group exits; join it.
    await anyio.to_thread.run_sync(exited.wait, 5.0)
    return log


type ThreadCMWrapper = Callable[[AbstractContextManager[str]], AbstractAsyncContextManager[str]]


@asynccontextmanager
async def naive_contextmanager_in_threadpool(
    cm: AbstractContextManager[str],
) -> AsyncGenerator[str]:
    """Pre-0.82.0 shape: __exit__ waits for a token from the shared default limiter."""
    value = await run_in_threadpool(cm.__enter__)
    try:
        yield value
    finally:
        await run_in_threadpool(cm.__exit__, None, None, None)


async def pool_teardown_scenario(wrapper: ThreadCMWrapper, *, pool_timeout: float) -> list[str]:
    """Request A holds a pooled connection; request B holds the only thread token waiting for it."""
    anyio.to_thread.current_default_thread_limiter().total_tokens = 1
    pool = threading.BoundedSemaphore(1)
    b_holds_token = anyio.Event()
    log: list[str] = []

    @contextmanager
    def connection() -> Generator[str]:
        pool.acquire()
        log.append("a:checked-out")
        try:
            yield "conn-a"
        finally:
            pool.release()
            log.append("a:returned-connection")

    def request_b_checkout() -> bool:
        anyio.from_thread.run_sync(b_holds_token.set)
        got = pool.acquire(timeout=pool_timeout)
        if got:
            pool.release()
        return got

    async def request_b() -> None:
        got = await anyio.to_thread.run_sync(request_b_checkout)
        log.append(f"b:checkout(acquired={got})")

    async with anyio.create_task_group() as tg, wrapper(connection()):
        tg.start_soon(request_b)
        await b_holds_token.wait()
        log.append("a:endpoint-done")
    return log
