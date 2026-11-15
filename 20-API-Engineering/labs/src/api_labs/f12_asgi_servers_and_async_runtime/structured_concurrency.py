"""Structured concurrency probes: AnyIO 4.16.0 task groups versus asyncio.TaskGroup.

Every probe runs a small task tree with no sockets and no wall-clock sleeps, records
lifecycle events in order, and handles the resulting exception group with ``except*``,
so tests can assert sibling cancellation, cleanup ordering, and group shape.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import anyio
import anyio.lowlevel

if TYPE_CHECKING:
    from anyio.abc import TaskStatus


class UpstreamError(Exception):
    """A child failure standing in for a failed upstream call."""


class QuotaError(Exception):
    """A second, independent failure type, used to show ``except*`` splitting."""


class _Terminate(Exception):
    """Sentinel raised to end an asyncio.TaskGroup early (pre-3.15 recipe)."""


@dataclass
class Trace:
    """Append-only event log shared by every task in one probe."""

    events: list[str] = field(default_factory=list[str])

    def add(self, event: str) -> None:
        self.events.append(event)


@dataclass(frozen=True)
class GroupReport:
    """What the code that owned the task group observed after it exited."""

    events: tuple[str, ...]
    handled: tuple[str, ...]
    parent_cancelling: int


def leaves(group: BaseExceptionGroup[BaseException]) -> list[BaseException]:
    """Flatten nested exception groups into their leaf exceptions, depth first."""
    out: list[BaseException] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            out.extend(leaves(cast("BaseExceptionGroup[BaseException]", exc)))
        else:
            out.append(exc)
    return out


def wrap_naked(exc: Exception) -> BaseExceptionGroup[Exception]:
    """Show that ``except*`` wraps a non-group exception in an ExceptionGroup."""
    caught: list[BaseExceptionGroup[Exception]] = []
    try:
        raise exc
    except* Exception as group:  # `return` is a SyntaxError inside except*
        caught.append(group)
    return caught[0]


def _current_cancelling() -> int:
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("probes must run inside an asyncio task")
    return task.cancelling()


# AnyIO side -----------------------------------------------------------------


async def _anyio_sibling(trace: Trace, name: str, shielded_cleanup: bool) -> None:
    trace.add(f"{name}:start")
    try:
        await anyio.sleep_forever()
    finally:
        trace.add(f"{name}:cancelled")
        if shielded_cleanup:
            with anyio.CancelScope(shield=True):
                await anyio.lowlevel.checkpoint()  # e.g. flush a buffer, release a lease
        else:
            await anyio.lowlevel.checkpoint()  # level cancellation: raises again right here
        trace.add(f"{name}:cleanup-done")


async def _anyio_failer(trace: Trace, name: str, exc: Exception) -> None:
    trace.add(f"{name}:start")
    await anyio.lowlevel.checkpoint()  # let every sibling reach its first await
    trace.add(f"{name}:raise")
    raise exc


async def anyio_sibling_cancellation(*, shielded_cleanup: bool) -> GroupReport:
    """One child fails; AnyIO cancels the siblings and the body, then raises a group."""
    trace = Trace()
    handled: list[str] = []
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_anyio_sibling, trace, "a", shielded_cleanup)
            tg.start_soon(_anyio_sibling, trace, "b", shielded_cleanup)
            tg.start_soon(_anyio_failer, trace, "f", UpstreamError("upstream 503"))
            try:
                await anyio.sleep_forever()
            finally:
                trace.add("body:cancelled")
    except* UpstreamError as group:
        handled.extend(type(e).__name__ for e in leaves(group))
    trace.add("owner:after-group")
    return GroupReport(tuple(trace.events), tuple(sorted(handled)), _current_cancelling())


async def anyio_two_failures() -> GroupReport:
    """Two children fail in the same loop iteration: both land in one group."""
    trace = Trace()
    handled: list[str] = []
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_anyio_failer, trace, "u", UpstreamError("upstream 503"))
            tg.start_soon(_anyio_failer, trace, "q", QuotaError("quota exhausted"))
    except* UpstreamError as group:
        handled.extend(f"upstream-clause:{type(e).__name__}" for e in leaves(group))
    except* QuotaError as group:
        handled.extend(f"quota-clause:{type(e).__name__}" for e in leaves(group))
    return GroupReport(tuple(trace.events), tuple(sorted(handled)), _current_cancelling())


async def anyio_nested_groups() -> tuple[int, GroupReport]:
    """A failure inside an inner group arrives at the outer owner two groups deep."""
    trace = Trace()
    handled: list[str] = []
    depth = 0

    async def inner() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_anyio_failer, trace, "inner", UpstreamError("deep"))

    try:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(inner)
        except BaseExceptionGroup as outer:
            first = outer.exceptions[0]
            depth = 2 if isinstance(first, BaseExceptionGroup) else 1
            raise
    except* UpstreamError as group:
        handled.extend(type(e).__name__ for e in leaves(group))
    return depth, GroupReport(tuple(trace.events), tuple(handled), _current_cancelling())


async def _return_after_checkpoint(value: int) -> int:
    await anyio.lowlevel.checkpoint()
    return value


async def anyio_handle_statuses() -> dict[str, str]:
    """AnyIO 4.14+ task handles report how each child of a failed group ended."""
    trace = Trace()
    handles: dict[str, anyio.TaskHandle[object]] = {}
    try:
        async with anyio.create_task_group() as tg:
            handles["finished"] = tg.create_task(_return_after_checkpoint(42))
            handles["sibling"] = tg.start_soon(anyio.sleep_forever)
            handles["failer"] = tg.start_soon(
                _anyio_failer, trace, "f", UpstreamError("upstream 503")
            )
    except* UpstreamError:
        pass
    statuses = {name: handle.status.name for name, handle in handles.items()}
    statuses["finished.return_value"] = str(handles["finished"].return_value)
    try:
        await handles["failer"]
    except anyio.TaskFailed as exc:
        statuses["await failer"] = f"TaskFailed from {type(exc.__cause__).__name__}"
    return statuses


async def _service(
    trace: Trace,
    fail_before_ready: bool,
    *,
    task_status: TaskStatus[int] = anyio.TASK_STATUS_IGNORED,
) -> None:
    trace.add("service:init")
    await anyio.lowlevel.checkpoint()
    if fail_before_ready:
        raise UpstreamError("bind failed")
    task_status.started(8080)
    trace.add("service:ready")
    await anyio.sleep_forever()


async def anyio_start_then_cancel(*, fail_before_ready: bool) -> GroupReport:
    """``tg.start()`` waits for readiness; a pre-ready failure raises bare in the caller."""
    trace = Trace()
    async with anyio.create_task_group() as tg:
        try:
            port = await tg.start(_service, trace, fail_before_ready)
        except UpstreamError as exc:
            trace.add(f"owner:start-raised {type(exc).__name__}")
        else:
            trace.add(f"owner:ready on {port}")
        tg.cancel()  # AnyIO 4.14+: same as tg.cancel_scope.cancel()
    trace.add("owner:after-group")
    return GroupReport(tuple(trace.events), (), _current_cancelling())


async def anyio_outer_deadline() -> tuple[str, GroupReport]:
    """An expired ``fail_after`` around a task group surfaces as a bare TimeoutError."""
    trace = Trace()
    outcome = "no-exception"
    try:
        with anyio.fail_after(0):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_anyio_sibling, trace, "a", True)
                await anyio.sleep_forever()
    except TimeoutError:
        outcome = "TimeoutError"
    except BaseExceptionGroup:
        outcome = "BaseExceptionGroup"
    return outcome, GroupReport(tuple(trace.events), (), _current_cancelling())


# asyncio side ---------------------------------------------------------------


async def _asyncio_sibling(trace: Trace, name: str) -> None:
    trace.add(f"{name}:start")
    try:
        await asyncio.Event().wait()
    finally:
        trace.add(f"{name}:cancelled")
        await asyncio.sleep(0)  # edge cancellation: the one CancelledError is spent
        trace.add(f"{name}:cleanup-done")


async def _asyncio_failer(trace: Trace, name: str, exc: Exception) -> None:
    trace.add(f"{name}:start")
    await asyncio.sleep(0)
    trace.add(f"{name}:raise")
    raise exc


async def asyncio_sibling_cancellation() -> GroupReport:
    """Same tree as the AnyIO probe, on asyncio.TaskGroup."""
    trace = Trace()
    handled: list[str] = []
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_asyncio_sibling(trace, "a"))
            tg.create_task(_asyncio_sibling(trace, "b"))
            tg.create_task(_asyncio_failer(trace, "f", UpstreamError("upstream 503")))
            try:
                await asyncio.Event().wait()
            finally:
                trace.add("body:cancelled")
    except* UpstreamError as group:
        handled.extend(type(e).__name__ for e in leaves(group))
    trace.add("owner:after-group")
    return GroupReport(tuple(trace.events), tuple(sorted(handled)), _current_cancelling())


async def asyncio_body_raises() -> GroupReport:
    """An exception in the ``async with`` body joins the children's errors in one group."""
    trace = Trace()
    handled: list[str] = []
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_asyncio_sibling(trace, "a"))
            await asyncio.sleep(0)
            trace.add("body:raise")
            raise QuotaError("body failed")
    except* QuotaError as group:
        handled.extend(type(e).__name__ for e in leaves(group))
    return GroupReport(tuple(trace.events), tuple(handled), _current_cancelling())


async def asyncio_first_result_wins() -> tuple[str, GroupReport]:
    """Portable early exit: raise a sentinel, then swallow exactly that sentinel."""
    trace = Trace()
    winner: list[str] = []

    async def fast() -> None:
        await asyncio.sleep(0)
        winner.append("fast")
        trace.add("fast:won")
        raise _Terminate

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_asyncio_sibling(trace, "slow"))
            tg.create_task(fast())
    except* _Terminate:
        trace.add("owner:terminated")
    return winner[0], GroupReport(tuple(trace.events), (), _current_cancelling())


async def asyncio_outer_timeout() -> tuple[str, GroupReport]:
    """An expired asyncio.timeout around a TaskGroup surfaces as a bare TimeoutError."""
    trace = Trace()
    outcome = "no-exception"
    try:
        async with asyncio.timeout(0), asyncio.TaskGroup() as tg:
            tg.create_task(_asyncio_sibling(trace, "a"))
            await asyncio.Event().wait()
    except TimeoutError:
        outcome = "TimeoutError"
    except BaseExceptionGroup:
        outcome = "BaseExceptionGroup"
    return outcome, GroupReport(tuple(trace.events), (), _current_cancelling())


# Race: an outside Task.cancel() lands in the same iteration as a child failure ---


async def _race_victim(runtime: Literal["anyio", "asyncio"], log: list[str]) -> None:
    parent = asyncio.current_task()
    if parent is None:
        raise RuntimeError("probes must run inside an asyncio task")

    async def boom() -> None:
        await asyncio.sleep(0)
        # An outside actor (watchdog, shutdown hook) cancels the request task; the
        # callback runs in the same loop iteration as the group's own failure handling.
        asyncio.get_running_loop().call_soon(parent.cancel)
        raise UpstreamError("upstream 503")

    try:
        if runtime == "anyio":
            async with anyio.create_task_group() as atg:
                atg.start_soon(boom)
                await anyio.sleep_forever()
        else:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(boom())
                await asyncio.Event().wait()
    except* UpstreamError:
        log.append("handled")
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        log.append("cancelled-at-next-await")
        raise
    log.append(f"continued with cancelling()={parent.cancelling()}")


async def outside_cancel_racing_child_failure(
    runtime: Literal["anyio", "asyncio"],
) -> tuple[tuple[str, ...], bool]:
    """Does the outside cancellation survive the group's exit, or is it swallowed?"""
    log: list[str] = []
    task = asyncio.create_task(_race_victim(runtime, log))
    try:
        await task
    except asyncio.CancelledError:
        log.append("owner:victim-cancelled")
    return tuple(log), task.cancelled()
