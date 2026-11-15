"""CPython asyncio BaseEventLoop internals inspector and tick tracer.

Instruments and inspects BaseEventLoop._run_once phases:
1. Cancelled timer heap purge (lazy amortization vs bulk heapify).
2. Selector timeout calculation (immediate zero vs deadline vs infinite).
3. Selector polling and I/O event registration into _ready deque.
4. Expired timer handle migration from _scheduled min-heap to _ready deque.
5. Batch execution of ready callbacks (ntodo starvation snapshot).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass
from typing import Any, Final, cast

# Exact constants from CPython Lib/asyncio/base_events.py
MIN_SCHEDULED_TIMER_HANDLES: Final[int] = 100
MIN_CANCELLED_TIMER_HANDLES_FRACTION: Final[float] = 0.5
MAXIMUM_SELECT_TIMEOUT: Final[float] = 24.0 * 3600.0


def stub_selector_select(loop: asyncio.AbstractEventLoop) -> None:
    """Make the loop's selector return no events at once, so a tick never blocks."""

    def no_events(timeout: float | None) -> list[Any]:
        return []

    cast("Any", loop)._selector.select = no_events


@dataclass(slots=True)
class TickRecord:
    """Snapshot of event loop internal metrics across one _run_once iteration."""

    tick_index: int
    scheduled_heap_size_start: int
    timer_cancelled_count_start: int
    selector_timeout: float | None
    ready_size_start: int
    ready_queue_size_end: int


class EventLoopInspector:
    """Introspects and traces BaseEventLoop internal data structures."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if loop is None:
            self.loop: asyncio.BaseEventLoop = asyncio.SelectorEventLoop()
            self._owns_loop = True
        else:
            if not isinstance(loop, asyncio.base_events.BaseEventLoop):
                raise TypeError("Inspector requires a BaseEventLoop instance")
            self.loop = loop
            self._owns_loop = False

        self.ticks: list[TickRecord] = []

    @property
    def ready_deque(self) -> Any:
        """Direct reference to loop._ready deque."""
        return cast("Any", self.loop)._ready

    @property
    def scheduled_heap(self) -> list[Any]:
        """Direct reference to loop._scheduled min-heap."""
        return cast("list[Any]", cast("Any", self.loop)._scheduled)

    @property
    def timer_cancelled_count(self) -> int:
        """Current loop._timer_cancelled_count tally."""
        return int(cast("Any", self.loop)._timer_cancelled_count)

    def close(self) -> None:
        """Detach instrumentation and close loop if owned."""
        if self._owns_loop and not self.loop.is_closed():
            self.loop.close()

    def get_ready_names(self) -> list[str]:
        """Extract human-readable representation of callbacks in _ready."""
        names: list[str] = []
        for handle in self.ready_deque:
            cb = getattr(handle, "_callback", None)
            if cb is not None:
                cb_name = getattr(cb, "__qualname__", getattr(cb, "__name__", str(cb)))
                names.append(cb_name)
            else:
                names.append("<cancelled>")
        return names

    def get_scheduled_deadlines(self) -> list[float]:
        """Extract sorted list of upcoming timer deadlines from min-heap."""
        # Heap list is partially ordered; sort a copy for inspection
        return sorted(handle._when for handle in self.scheduled_heap)

    def is_purge_threshold_exceeded(self) -> bool:
        """Check if cancelled timers exceed bulk heapify threshold."""
        sched_count = len(self.scheduled_heap)
        if sched_count <= MIN_SCHEDULED_TIMER_HANDLES:
            return False
        return self.timer_cancelled_count / sched_count > MIN_CANCELLED_TIMER_HANDLES_FRACTION

    def step_tick(self) -> TickRecord:
        """Execute exactly one iteration of _run_once with telemetry tracking."""
        tick_idx = len(self.ticks)
        sched_start = len(self.scheduled_heap)
        canc_start = self.timer_cancelled_count

        # 1. Determine expected selector timeout exactly as _run_once does
        expected_timeout: float | None = None
        if self.ready_deque or getattr(self.loop, "_stopping", False):
            expected_timeout = 0.0
        elif self.scheduled_heap:
            timeout_val = self.scheduled_heap[0]._when - self.loop.time()
            if timeout_val > MAXIMUM_SELECT_TIMEOUT:
                expected_timeout = MAXIMUM_SELECT_TIMEOUT
            elif timeout_val < 0.0:
                expected_timeout = 0.0
            else:
                expected_timeout = timeout_val
        else:
            expected_timeout = None

        ready_before = len(self.ready_deque)

        # Execute the real _run_once
        self.loop._run_once()  # type: ignore[attr-defined]

        record = TickRecord(
            tick_index=tick_idx,
            scheduled_heap_size_start=sched_start,
            timer_cancelled_count_start=canc_start,
            selector_timeout=expected_timeout,
            ready_size_start=ready_before,
            ready_queue_size_end=len(self.ready_deque),
        )
        self.ticks.append(record)
        return record


def simulate_tick_ordering(
    *,
    has_call_soon: bool = True,
    has_io_event: bool = True,
    has_expired_timer: bool = True,
    has_child_call_soon: bool = True,
) -> list[str]:
    """Demonstrates exact Phase 3, Phase 4, and Phase 5 ordering in one tick.

    Returns the sequential trace of executed callback labels.
    """
    loop = asyncio.new_event_loop()
    try:
        execution_order: list[str] = []

        # Setup mock selector returning pre-canned event if requested
        if has_io_event:
            # We use an actual loop reader on a dummy socketpair for realism
            rsock, wsock = socket.socketpair()
            rsock.setblocking(False)
            wsock.setblocking(False)

            def io_callback() -> None:
                execution_order.append("phase3:io_read")
                with contextlib.suppress(BlockingIOError):
                    rsock.recv(1024)
                if has_child_call_soon:
                    loop.call_soon(lambda: execution_order.append("phase5_child:call_soon_from_io"))

            loop.add_reader(rsock.fileno(), io_callback)
            wsock.send(b"1")
        else:
            rsock, wsock = None, None

        if has_call_soon:
            loop.call_soon(lambda: execution_order.append("phase5_init:call_soon_1"))

        if has_expired_timer:
            # Timer scheduled at current loop time (expires in Phase 4)
            loop.call_later(0.0, lambda: execution_order.append("phase4:timer_expired"))

        # Run exactly one tick
        loop._run_once()  # type: ignore[attr-defined]

        if rsock is not None and wsock is not None:
            loop.remove_reader(rsock.fileno())
            rsock.close()
            wsock.close()

        return execution_order
    finally:
        loop.close()


def verify_timer_heap_cancellation(
    total_timers: int,
    cancel_count: int,
    cancel_head: bool,
) -> tuple[int, int]:
    """Runs one tick with dummy selector to observe timer cancellation purge.

    Returns:
        (scheduled_heap_len_after, timer_cancelled_count_after)
    """
    loop = asyncio.new_event_loop()
    try:
        # Mock selector.select so it returns immediately without blocking
        stub_selector_select(loop)

        handles = [loop.call_later(100.0 + i, lambda: None) for i in range(total_timers)]

        if cancel_head and handles:
            handles[0].cancel()
            # Cancel requested remaining from tail
            for h in handles[1:cancel_count]:
                h.cancel()
        else:
            # Cancel from tail backwards, leaving head uncancelled
            for h in handles[len(handles) - cancel_count :]:
                h.cancel()

        loop._run_once()  # type: ignore[attr-defined]
        return (len(loop._scheduled), loop._timer_cancelled_count)  # type: ignore[attr-defined]
    finally:
        loop.close()
