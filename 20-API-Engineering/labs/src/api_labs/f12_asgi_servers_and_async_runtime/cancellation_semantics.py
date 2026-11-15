"""Cancellation mechanics: Task.cancel(), cancelling(), swallowing, and shielded finalizers.

Every probe advances the event loop with zero-length sleeps, so its result does not depend
on wall-clock timing; the one exception is the bounded-shield probe, whose rollback never
finishes and is abandoned by a short AnyIO deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import anyio
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from starlette.requests import Request


class UpstreamError(Exception):
    """A dependency failed (for example an upstream `503`)."""


def _current() -> asyncio.Task[Any]:
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("probes must run inside an asyncio task")
    return task


async def _until(predicate: Callable[[], bool], limit: int = 100) -> None:
    """Spin the loop one iteration at a time until `predicate()` holds."""
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


# --- Task.cancel() and the cancellation counter ------------------------------------------


@dataclass(frozen=True)
class CancelRequestReport:
    returned: tuple[bool, ...]  # return value of each cancel() call
    deliveries: int  # CancelledError instances the coroutine actually caught
    message: str | None  # args[0] of the delivered CancelledError
    cancelling_after: int  # Task.cancelling() after the coroutine recovered
    task_cancelled: bool


async def coalesced_cancel_requests(messages: tuple[str, ...]) -> CancelRequestReport:
    """Call cancel() once per message on a parked task; count what the coroutine sees."""
    caught: list[asyncio.CancelledError] = []
    after: list[int] = []

    async def victim() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError as exc:
            caught.append(exc)  # swallowed on purpose so the counter can be observed
        await asyncio.sleep(0)  # edge-triggered: this await is not cancelled again
        after.append(_current().cancelling())

    task = asyncio.create_task(victim())
    await asyncio.sleep(0)  # park the victim on its sleep future
    returned = tuple(task.cancel(msg) for msg in messages)
    await task
    message = str(caught[0].args[0]) if caught and caught[0].args else None
    return CancelRequestReport(returned, len(caught), message, after[0], task.cancelled())


async def cancel_before_first_step() -> tuple[bool, bool, bool]:
    """Cancel a task before it ever ran: (body ran, finally ran, task.cancelled())."""
    ran: list[str] = []

    async def victim() -> None:
        try:
            ran.append("body")
            await asyncio.sleep(0)
        finally:
            ran.append("finally")

    task = asyncio.create_task(victim())
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return "body" in ran, "finally" in ran, task.cancelled()


async def uncancel_before_first_step() -> bool:
    """cancel() then uncancel() before the first step; return True if the body ran."""
    ran: list[str] = []

    async def victim() -> None:
        ran.append("body")

    task = asyncio.create_task(victim())
    task.cancel()
    task.uncancel()  # 3.13+ rescinds the pending throw when the count reaches 0
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return bool(ran)


async def swallow_and_return() -> tuple[str, bool, int]:
    """Swallow a cancel without uncancel(): (result, task.cancelled(), cancelling())."""

    async def victim() -> str:
        try:  # noqa: SIM105 - spelled out on purpose: this is the bug
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass  # no re-raise and no uncancel()
        return "value"

    task = asyncio.create_task(victim())
    await asyncio.sleep(0)
    task.cancel()
    result = await task
    return result, task.cancelled(), task.cancelling()


# --- Swallowing CancelledError breaks asyncio.timeout() ----------------------------------

Handler = Literal["except-exception", "except-baseexception", "except-baseexception-reraise"]
HANDLERS: dict[str, Handler] = {
    "except-exception": "except-exception",
    "except-baseexception": "except-baseexception",
    "except-baseexception-reraise": "except-baseexception-reraise",
}


async def _upstream_call() -> None:
    await asyncio.sleep(0)  # stands in for an HTTP or database round trip


async def retry_loop(handler: Handler, log: list[str], attempts: int = 3) -> None:
    """A retry loop whose error handler is the variable under test."""
    for i in range(attempts):
        if handler == "except-exception":
            try:
                await _upstream_call()
                log.append(f"ok:{i}")
            except Exception:
                log.append(f"error:{i}")  # CancelledError is not an Exception: never here
        elif handler == "except-baseexception":
            try:
                await _upstream_call()
                log.append(f"ok:{i}")
            except BaseException:  # the bug; a bare `except:` behaves identically
                log.append(f"swallowed:{i}")
        else:
            try:
                await _upstream_call()
                log.append(f"ok:{i}")
            except BaseException:
                log.append(f"cleanup:{i}")
                raise


@dataclass(frozen=True)
class DeadlineReport:
    outcome: Literal["timeout", "completed"]
    calls: tuple[str, ...]
    cancelling_after: int


async def call_with_deadline(handler: Handler) -> DeadlineReport:
    """Run the retry loop under an already-due `asyncio.timeout(0)`."""
    log: list[str] = []
    outcome: Literal["timeout", "completed"] = "completed"
    try:
        async with asyncio.timeout(0):  # fires on the next loop iteration
            await retry_loop(handler, log)
    except TimeoutError:
        outcome = "timeout"
    return DeadlineReport(outcome, tuple(log), _current().cancelling())


def build_app() -> Starlette:
    """`GET /report?handler=...` maps a deadline to `504`, so a swallow shows up as `200`."""

    async def report(request: Request) -> JSONResponse:
        handler = HANDLERS.get(request.query_params.get("handler", "except-exception"))
        if handler is None:
            return JSONResponse({"error": "unknown handler"}, status_code=400)
        result = await call_with_deadline(handler)
        status = 504 if result.outcome == "timeout" else 200
        return JSONResponse({"calls": list(result.calls)}, status_code=status)

    return Starlette(routes=[Route("/report", report)])


# --- A swallowed cancel resurfaces at the next TaskGroup ---------------------------------


@dataclass(frozen=True)
class ResurrectionReport:
    log: tuple[str, ...]
    task_cancelled: bool
    result: str | None


async def swallowed_cancel_then_task_group(*, uncancel: bool) -> ResurrectionReport:
    """Swallow an outside cancel, then run a TaskGroup whose child fails."""
    log: list[str] = []

    async def failing_child() -> None:
        await asyncio.sleep(0)
        raise UpstreamError("upstream 503")

    async def request_task() -> str:
        try:
            await asyncio.sleep(3600)  # waiting on a slow upstream when the cancel lands
        except asyncio.CancelledError:
            log.append("swallowed")
            if uncancel:
                _current().uncancel()
        log.append(f"cancelling={_current().cancelling()}")
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(failing_child())
                await asyncio.sleep(3600)
        except* UpstreamError:
            log.append("group-handled")
        log.append("returning")
        return "fallback-response"

    task = asyncio.create_task(request_task())
    await asyncio.sleep(0)
    task.cancel()
    result: str | None
    try:
        result = await task
    except asyncio.CancelledError:
        result = None
    return ResurrectionReport(tuple(log), task.cancelled(), result)


# --- Finalizers: rolling back a transaction while being cancelled ------------------------

ConnState = Literal["idle", "in-transaction", "rolling-back", "clean", "invalidated"]


@dataclass
class FakeConnection:
    """In-memory stand-in for a pooled DB connection; rollback takes several iterations."""

    rollback_steps: int | None = 3  # None: the rollback never completes (dead peer)
    state: ConnState = "idle"
    trace: list[str] = field(default_factory=list[str])
    background: asyncio.Task[None] | None = None

    async def begin(self) -> None:
        await asyncio.sleep(0)
        self.state = "in-transaction"
        self.trace.append("begin")

    async def rollback(self) -> None:
        self.state = "rolling-back"
        self.trace.append("rollback:start")
        if self.rollback_steps is None:
            await asyncio.Event().wait()
        else:
            for _ in range(self.rollback_steps):
                await asyncio.sleep(0)
        self.state = "clean"
        self.trace.append("rollback:done")

    def invalidate(self) -> None:
        self.state = "invalidated"
        self.trace.append("invalidate")


@dataclass
class FakePool:
    released: list[ConnState] = field(default_factory=list[ConnState])

    def release(self, conn: FakeConnection) -> None:
        conn.trace.append(f"release:{conn.state}")
        self.released.append(conn.state)


async def run_to_completion[T](coro: Coroutine[Any, Any, T]) -> T:
    """Finish `coro` even if the caller is cancelled meanwhile, then re-raise.

    Same shape as SQLAlchemy 2.1.8 `sqlalchemy/ext/asyncio/base.py`, `_run_to_completion`.
    """
    task = asyncio.create_task(coro)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as err:
            if task.done():
                raise
            cancelled = err  # remember it, keep waiting for the cleanup
        else:
            break
    if cancelled is not None:
        raise cancelled
    return result


Finalizer = Literal["bare-await", "asyncio-shield", "run-to-completion", "anyio-shield"]


async def _handle_request(conn: FakeConnection, pool: FakePool, finalizer: Finalizer) -> None:
    await conn.begin()
    try:
        await asyncio.sleep(3600)  # a slow query, interrupted by the request deadline
    finally:
        try:
            if finalizer == "bare-await":
                await conn.rollback()
            elif finalizer == "asyncio-shield":
                conn.background = asyncio.create_task(conn.rollback())  # keep a reference
                await asyncio.shield(conn.background)
            elif finalizer == "run-to-completion":
                await run_to_completion(conn.rollback())
            else:
                with anyio.CancelScope(shield=True):
                    await conn.rollback()
        finally:
            pool.release(conn)


@dataclass(frozen=True)
class FinalizerReport:
    trace: tuple[str, ...]
    released: tuple[ConnState, ...]
    final_state: ConnState
    task_cancelled: bool


async def double_cancel_during_cleanup(finalizer: Finalizer) -> FinalizerReport:
    """Deadline cancel, then a second native cancel while the rollback is in flight.

    The second cancel is what Uvicorn 0.54.0 `Server.shutdown` sends to every in-flight
    task once `timeout_graceful_shutdown` expires.
    """
    conn, pool = FakeConnection(), FakePool()
    task = asyncio.create_task(_handle_request(conn, pool, finalizer))
    await _until(lambda: conn.state == "in-transaction")
    task.cancel("request deadline")
    await _until(lambda: conn.state == "rolling-back")
    task.cancel("Task cancelled, timeout graceful shutdown exceeded")
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if conn.background is not None:
        await conn.background  # the shielded rollback is still running on its own
    return FinalizerReport(tuple(conn.trace), tuple(pool.released), conn.state, task.cancelled())


AnyioFinalizer = Literal["bare-await", "shielded", "shielded-bounded"]


async def anyio_scope_cancel_during_cleanup(
    finalizer: AnyioFinalizer, *, rollback_steps: int | None = 3
) -> FinalizerReport:
    """Cancel an AnyIO task group while its child holds an open transaction."""
    conn, pool = FakeConnection(rollback_steps=rollback_steps), FakePool()

    async def child() -> None:
        await conn.begin()
        try:
            await anyio.sleep_forever()
        finally:
            try:
                if finalizer == "bare-await":
                    await conn.rollback()  # level cancellation: raises at the first await
                elif finalizer == "shielded":
                    with anyio.CancelScope(shield=True):
                        await conn.rollback()
                else:
                    with anyio.move_on_after(0.05, shield=True) as scope:
                        await conn.rollback()
                    if scope.cancelled_caught:
                        conn.invalidate()  # never return a half-rolled-back connection
            finally:
                pool.release(conn)

    async with anyio.create_task_group() as tg:
        tg.start_soon(child)
        await _until(lambda: conn.state == "in-transaction")
        tg.cancel_scope.cancel()
    return FinalizerReport(tuple(conn.trace), tuple(pool.released), conn.state, False)


# --- What happens to an exception raised by shielded work after the caller left ----------


async def shielded_failure_after_outer_cancel() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (messages logged while running, messages logged at garbage collection)."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    messages: list[str] = []

    def handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        messages.append(str(context["message"]))

    loop.set_exception_handler(handler)
    try:
        gate = asyncio.Event()

        async def cleanup() -> None:
            await gate.wait()
            raise RuntimeError("rollback failed")

        inner = asyncio.create_task(cleanup())

        async def caller(work: asyncio.Task[None]) -> None:
            await asyncio.shield(work)

        outer = asyncio.create_task(caller(inner))
        await asyncio.sleep(0)
        outer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await outer
        gate.set()  # the shielded cleanup now fails, after its caller is gone
        for _ in range(3):
            await asyncio.sleep(0)
        eager = tuple(messages)
        del inner, outer  # drop the last references so the failed task is finalized
        gc.collect()
        await asyncio.sleep(0)
        return eager, tuple(messages[len(eager) :])
    finally:
        loop.set_exception_handler(previous)
