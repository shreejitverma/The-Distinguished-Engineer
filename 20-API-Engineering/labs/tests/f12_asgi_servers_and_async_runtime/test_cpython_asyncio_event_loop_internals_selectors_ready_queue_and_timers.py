"""Tests for CPython asyncio Event Loop Internals: BaseEventLoop._run_once,

epoll/kqueue Selector Polling, _ready Deque, and _scheduled Min-Heap.
"""

from __future__ import annotations

import contextlib
from asyncio import base_events
from typing import TYPE_CHECKING, Any

import pytest

from api_labs.f12_asgi_servers_and_async_runtime.event_loop_inspector import (
    MAXIMUM_SELECT_TIMEOUT,
    MIN_CANCELLED_TIMER_HANDLES_FRACTION,
    MIN_SCHEDULED_TIMER_HANDLES,
    EventLoopInspector,
    simulate_tick_ordering,
    stub_selector_select,
    verify_timer_heap_cancellation,
)

if TYPE_CHECKING:
    import asyncio
    import weakref


@pytest.mark.unit
def test_call_soon_fifo_and_ntodo_starvation_prevention() -> None:
    """Verify ready queue FIFO order and ntodo batching preventing tick starvation."""
    inspector = EventLoopInspector()
    try:
        loop = inspector.loop
        trace: list[str] = []

        def cb1() -> None:
            trace.append("cb1")
            # Schedule a child callback during tick 1
            loop.call_soon(cb3)

        def cb2() -> None:
            trace.append("cb2")

        def cb3() -> None:
            trace.append("cb3")

        loop.call_soon(cb1)
        loop.call_soon(cb2)

        # Before tick 1: _ready contains cb1 and cb2
        assert len(inspector.ready_deque) == 2

        # Step tick 1
        inspector.step_tick()

        # cb1 and cb2 ran; cb3 was enqueued into _ready but deferred to tick 2
        assert trace == ["cb1", "cb2"]
        assert len(inspector.ready_deque) == 1

        # Step tick 2
        inspector.step_tick()

        # cb3 has now executed
        assert trace == ["cb1", "cb2", "cb3"]
        assert len(inspector.ready_deque) == 0
    finally:
        inspector.close()


@pytest.mark.unit
def test_run_once_phase_ordering_call_soon_io_and_timer() -> None:
    """Verify execution ordering within one tick: pre-existing call_soon -> I/O -> timer.

    Child callbacks scheduled during I/O are deferred to the subsequent tick.
    """
    trace = simulate_tick_ordering(
        has_call_soon=True,
        has_io_event=True,
        has_expired_timer=True,
        has_child_call_soon=True,
    )

    # Invariant: Phase 5 runs pre-existing call_soon, then Phase 3 I/O readiness,
    # then Phase 4 expired timers. Child call_soon is NOT in tick 1 trace.
    assert trace == [
        "phase5_init:call_soon_1",
        "phase3:io_read",
        "phase4:timer_expired",
    ]


@pytest.mark.unit
def test_timer_lazy_cancellation_head_purge() -> None:
    """Verify that cancelled timer handles at the head of _scheduled are popped immediately."""
    heap_len_after, cancelled_count_after = verify_timer_heap_cancellation(
        total_timers=10,
        cancel_count=3,
        cancel_head=True,
    )
    # The head handle was cancelled, so while loop popped cancelled head handles
    assert cancelled_count_after == 0
    assert heap_len_after == 7


@pytest.mark.unit
def test_timer_lazy_cancellation_under_100_handles_retained() -> None:
    """Verify memory retention when <= 100 handles are scheduled and head is uncancelled."""
    heap_len_after, cancelled_count_after = verify_timer_heap_cancellation(
        total_timers=99,
        cancel_count=90,
        cancel_head=False,
    )
    # Since total_timers <= 100, the bulk heapify threshold is not met,
    # and since the head was not cancelled, all 90 cancelled handles remain in the heap!
    assert heap_len_after == 99
    assert cancelled_count_after == 90


@pytest.mark.unit
def test_timer_bulk_heapify_threshold_exceeded() -> None:
    """Verify bulk heapify occurs when total > 100 and cancelled fraction > 0.5."""
    heap_len_after, cancelled_count_after = verify_timer_heap_cancellation(
        total_timers=102,
        cancel_count=52,
        cancel_head=False,
    )
    # The lab constants mirror CPython's private thresholds exactly.
    cpython = vars(base_events)
    assert cpython["_MIN_SCHEDULED_TIMER_HANDLES"] == MIN_SCHEDULED_TIMER_HANDLES
    assert cpython["_MIN_CANCELLED_TIMER_HANDLES_FRACTION"] == MIN_CANCELLED_TIMER_HANDLES_FRACTION
    assert cpython["MAXIMUM_SELECT_TIMEOUT"] == MAXIMUM_SELECT_TIMEOUT
    # Total was 102 (> 100) and 52 cancelled (> 50% fraction)
    # Bulk heapify purged all 52 cancelled handles
    assert heap_len_after == 50
    assert cancelled_count_after == 0


@pytest.mark.unit
def test_selector_timeout_calculation() -> None:
    """Verify dynamic selector timeout calculation across loop states."""
    inspector = EventLoopInspector()
    try:
        loop = inspector.loop
        stub_selector_select(loop)

        # Case 1: _ready is non-empty -> timeout must be 0.0
        loop.call_soon(lambda: None)
        record = inspector.step_tick()
        assert record.selector_timeout == 0.0

        # Case 2: _scheduled is non-empty, _ready is empty -> timeout must match deadline
        handle = loop.call_later(15.0, lambda: None)
        record = inspector.step_tick()
        assert record.selector_timeout is not None
        assert 14.0 <= record.selector_timeout <= 15.1
        handle.cancel()
        inspector.step_tick()

        # Case 3: Both queues empty -> timeout must be None (infinite block)
        record = inspector.step_tick()
        assert record.selector_timeout is None
    finally:
        inspector.close()


@pytest.mark.unit
def test_clock_resolution_and_monotonicity() -> None:
    """Verify loop._clock_resolution adheres to monotonic clock metadata."""
    inspector = EventLoopInspector()
    try:
        import time

        mono_info = time.get_clock_info("monotonic")
        loop_res = inspector.loop._clock_resolution  # type: ignore[attr-defined]
        assert loop_res == mono_info.resolution
        assert loop_res > 0.0
    finally:
        inspector.close()


@pytest.mark.unit
def test_self_pipe_socketpair_wakeup() -> None:
    """Verify call_soon_threadsafe writes a null byte to self-pipe socketpair."""
    inspector = EventLoopInspector()
    try:
        loop = inspector.loop
        # Access internal self-pipe client socket
        csock = getattr(loop, "_csock", None)
        ssock = getattr(loop, "_ssock", None)

        if csock is not None and ssock is not None:
            # Drain any prior noise
            with contextlib.suppress(BlockingIOError):
                ssock.recv(4096)

            # Schedule threadsafe callback
            loop.call_soon_threadsafe(lambda: None)

            # ssock should have received b'\0'
            data = ssock.recv(1)
            assert data == b"\x00"

            # Execute the tick to clean up the ready handle
            inspector.step_tick()
    finally:
        inspector.close()


@pytest.mark.unit
def test_timer_handle_comparison_and_tie_breaking() -> None:
    """Verify TimerHandle comparison semantics when deadlines are identical."""
    inspector = EventLoopInspector()
    try:
        loop = inspector.loop
        when = loop.time() + 10.0

        h1 = loop.call_at(when, lambda: None)
        h2 = loop.call_at(when, lambda: None)

        # Neither is strictly less than the other (equality on when)
        assert not (h1 < h2)
        assert not (h2 < h1)

        h1.cancel()
        h2.cancel()
    finally:
        inspector.close()


@pytest.mark.unit
def test_handle_exception_isolation() -> None:
    """Verify callback exception is routed to loop exception handler without aborting tick."""
    inspector = EventLoopInspector()
    try:
        loop = inspector.loop
        handled_errors: list[dict[str, Any]] = []

        def custom_handler(_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
            handled_errors.append(ctx)

        loop.set_exception_handler(custom_handler)

        executed: list[str] = []

        def failing_cb() -> None:
            executed.append("fail")
            raise ValueError("Deliberate failure in callback")

        def surviving_cb() -> None:
            executed.append("survived")

        loop.call_soon(failing_cb)
        loop.call_soon(surviving_cb)

        # Run tick
        inspector.step_tick()

        # Both ran: the exception did not abort the second callback in ntodo
        assert executed == ["fail", "survived"]
        assert len(handled_errors) == 1
        assert isinstance(handled_errors[0]["exception"], ValueError)
    finally:
        inspector.close()


@pytest.mark.unit
def test_cancel_releases_callback_but_handle_stays_in_heap() -> None:
    """Verify Handle.cancel() drops the callback reference while the TimerHandle lingers."""
    import gc
    import weakref

    class Payload:
        pass

    inspector = EventLoopInspector()
    try:
        loop = inspector.loop

        def schedule() -> tuple[asyncio.TimerHandle, weakref.ReferenceType[Payload]]:
            payload = Payload()

            def holds_payload() -> Payload:
                return payload

            return loop.call_later(1000.0, holds_payload), weakref.ref(payload)

        handle, ref = schedule()
        filler = loop.call_later(1.0, lambda: None)  # uncancelled head keeps handle buried
        gc.collect()
        # Before cancel the handle's callback closure keeps the payload alive.
        assert ref() is not None
        handle.cancel()
        gc.collect()

        # The closure (and the payload it captured) is freed at cancel() time ...
        assert ref() is None
        # ... but the cancelled TimerHandle shell is still in _scheduled.
        assert handle in inspector.scheduled_heap
        assert inspector.timer_cancelled_count == 1
        filler.cancel()
    finally:
        inspector.close()


@pytest.mark.unit
def test_equal_deadline_timers_are_not_fifo() -> None:
    """Verify heapq gives no FIFO guarantee for TimerHandles with identical deadlines."""
    inspector = EventLoopInspector()
    try:
        loop = inspector.loop
        stub_selector_select(loop)
        when = loop.time() - 1.0
        order: list[int] = []
        for i in range(8):
            loop.call_at(when, order.append, i)
        inspector.step_tick()

        assert sorted(order) == list(range(8))
        assert order != list(range(8))
    finally:
        inspector.close()
