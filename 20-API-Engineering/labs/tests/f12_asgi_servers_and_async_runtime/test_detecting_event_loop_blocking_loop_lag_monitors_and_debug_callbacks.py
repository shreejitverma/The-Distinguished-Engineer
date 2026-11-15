"""Lab: detecting event-loop blocking with a lag canary, a stack-sampling watchdog,
debug-mode slow-callback logging, and faulthandler.

Every stall here is a deliberate 200 ms ``time.sleep`` on the loop thread checked
against a 50 ms threshold, so scheduler noise has a 4x margin.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from typing import TYPE_CHECKING

import httpx
import pytest
import uvloop

from api_labs.f12_asgi_servers_and_async_runtime.loop_lag_monitor import (
    BLOCK_SECONDS,
    DEFAULT_BLOCK_THRESHOLD,
    BlockingEvent,
    StackFrame,
    build_app,
    capture_slow_callback_warnings,
    corrected_lag_samples,
    faulthandler_dead_mans_switch,
    gil_enabled,
    hold_the_gil_in_c,
    monitor_event_loop,
    percentile,
    step_that_blocks,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

STALL = 0.200
LOOPS: dict[str, Callable[[], asyncio.AbstractEventLoop] | None] = {
    "asyncio": None,
    "uvloop": uvloop.new_event_loop,
}


def _block_here(seconds: float) -> None:
    time.sleep(seconds)  # the line the watchdog must name


def _slow_steps(
    duration: float,
    *,
    debug: bool,
    threshold: float,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
) -> list[str]:
    messages = capture_slow_callback_warnings(
        lambda: step_that_blocks(duration),
        debug=debug,
        slow_callback_duration=threshold,
        loop_factory=loop_factory,
    )
    return [m for m in messages if "coro=<step_that_blocks()" in m]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app()), base_url="http://test")


@pytest.mark.unit
def test_corrected_lag_backfills_one_sample_per_missed_wakeup() -> None:
    assert corrected_lag_samples(0.050, 0.010) == [0.05, 0.04, 0.03, 0.02, 0.01]
    assert corrected_lag_samples(0.004, 0.010) == [0.004]
    with pytest.raises(ValueError, match="positive"):
        corrected_lag_samples(0.05, 0.0)


@pytest.mark.unit
def test_percentile_is_nearest_rank() -> None:
    samples = [float(i) for i in range(1, 101)]
    assert (percentile(samples, 50), percentile(samples, 99), percentile(samples, 100)) == (
        50.0,
        99.0,
        100.0,
    )
    with pytest.raises(ValueError, match="no samples"):
        percentile([], 50)


@pytest.mark.unit
def test_one_long_stall_hides_from_raw_p99_but_not_from_corrected_p99() -> None:
    raw = [0.001] * 99 + [0.500]
    corrected = [s for lag in raw for s in corrected_lag_samples(lag, 0.010)]
    assert percentile(raw, 99) == 0.001
    assert len(corrected) == 149  # 49 wake-ups the stalled canary never took
    assert percentile(corrected, 99) == pytest.approx(0.490)


@pytest.mark.unit
@pytest.mark.parametrize("loop_name", sorted(LOOPS))
def test_slow_callback_warning_needs_debug_mode(loop_name: str) -> None:
    factory = LOOPS[loop_name]
    assert _slow_steps(0.120, debug=False, threshold=0.050, loop_factory=factory) == []
    (message,) = _slow_steps(0.120, debug=True, threshold=0.050, loop_factory=factory)
    took = re.search(r"took (\d+\.\d{3}) seconds$", message)
    assert took is not None
    assert float(took.group(1)) >= 0.120


@pytest.mark.unit
@pytest.mark.parametrize("loop_name", sorted(LOOPS))
def test_slow_callback_threshold_is_respected(loop_name: str) -> None:
    factory = LOOPS[loop_name]
    assert _slow_steps(0.020, debug=True, threshold=0.200, loop_factory=factory) == []


@pytest.mark.unit
def test_slow_callback_names_the_next_await_line_not_the_blocking_line() -> None:
    lines, first = inspect.getsourcelines(step_that_blocks)
    blocking = first + next(i for i, ln in enumerate(lines) if "time.sleep" in ln)
    parked = first + next(i for i, ln in enumerate(lines) if "await asyncio.sleep(0)" in ln)
    (message,) = _slow_steps(0.120, debug=True, threshold=0.050)
    assert "coro=<step_that_blocks() running at " in message
    assert f"loop_lag_monitor.py:{parked}>" in message
    assert f"loop_lag_monitor.py:{blocking}>" not in message


@pytest.mark.unit
def test_a_capture_parked_in_select_is_a_wakeup_delay_not_blocking_code() -> None:
    loop_frames = (StackFrame("base_events.py", 1, "_run_once"),)
    parked = BlockingEvent(0.06, (*loop_frames, StackFrame("/x/selectors.py", 548, "select")))
    busy = BlockingEvent(0.06, (*loop_frames, StackFrame("/app/views.py", 7, "select")))
    assert parked.parked_in_selector
    assert not busy.parked_in_selector  # same name, user code: still blocking
    assert not BlockingEvent(0.06, ()).parked_in_selector


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watchdog_names_the_blocking_line_while_it_blocks() -> None:
    async with monitor_event_loop() as health:
        await asyncio.sleep(0.05)
        _block_here(STALL)
        await asyncio.sleep(0.05)
    assert health.max_lag() >= STALL - DEFAULT_BLOCK_THRESHOLD
    (event,) = health.blocking_events()
    assert event.innermost.function == "_block_here"
    assert DEFAULT_BLOCK_THRESHOLD <= event.stalled_for < STALL
    assert "_run_once" in [frame.function for frame in event.stack]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idle_loop_reports_nothing() -> None:
    async with monitor_event_loop() as health:
        await asyncio.sleep(0.2)
    assert health.blocking_events() == []  # a late OS wake-up lands in wakeup_delays()
    assert len(health.monitor.raw) >= 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watchdog_reports_each_stall_once() -> None:
    async with monitor_event_loop() as health:
        for _ in range(2):
            await asyncio.sleep(0.05)
            _block_here(STALL)
        await asyncio.sleep(0.05)
    assert [e.innermost.function for e in health.blocking_events()] == ["_block_here"] * 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocking_async_endpoint_is_caught_with_its_stack() -> None:
    async with _client() as client, monitor_event_loop() as health:
        await asyncio.sleep(0.05)
        response = await client.get("/block")
        await asyncio.sleep(0.05)
    assert response.text == "blocked the loop"
    (event,) = health.blocking_events()
    assert event.innermost.function == "blocking_endpoint"
    assert health.max_lag() >= BLOCK_SECONDS - DEFAULT_BLOCK_THRESHOLD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_endpoint_runs_in_a_worker_thread_and_leaves_the_loop_free() -> None:
    async with _client() as client:
        await client.get("/offload")  # warm-up: anyio imports its backend on first use
        async with monitor_event_loop() as health:
            await asyncio.sleep(0.05)
            response = await client.get("/offload")
            await asyncio.sleep(0.05)
    assert response.text == "ran in a worker thread"
    assert health.blocking_events() == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_timer_inside_the_app_hides_the_stall() -> None:
    async with _client() as client:
        start = time.monotonic()

        async def timed(path: str) -> tuple[float, float]:
            began = time.monotonic()
            await client.get(path)
            done = time.monotonic()
            return done - began, done - start

        (_, _), (fast_own, fast_from_start) = await asyncio.gather(timed("/block"), timed("/fast"))
    assert fast_own < DEFAULT_BLOCK_THRESHOLD  # what an in-handler timer would record
    assert fast_from_start >= BLOCK_SECONDS  # what the client waited


@pytest.mark.unit
@pytest.mark.asyncio
async def test_faulthandler_switch_dumps_the_blocked_stack(tmp_path: Path) -> None:
    async def run(stall: float) -> str:
        path = tmp_path / f"dump-{stall}.txt"
        with path.open("w+") as sink:
            switch = asyncio.create_task(
                faulthandler_dead_mans_switch(sink, dump_after=0.100, interval=0.010)
            )
            await asyncio.sleep(0.03)
            _block_here(stall)
            await asyncio.sleep(0.03)
            switch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await switch
        return path.read_text()

    assert await run(0.0) == ""
    dump = await run(0.300)
    assert dump.startswith("Timeout (0:00:00.100000)!\n")
    assert "most recent call first" in dump
    assert "in _block_here" in dump


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_c_call_that_never_yields_defeats_the_python_watchdog_not_faulthandler(
    tmp_path: Path,
) -> None:
    lines, first = inspect.getsourcelines(hold_the_gil_in_c)
    c_line = first + next(i for i, ln in enumerate(lines) if "sum(range" in ln)
    path = tmp_path / "dump.txt"
    with path.open("w+") as sink:
        switch = asyncio.create_task(
            faulthandler_dead_mans_switch(sink, dump_after=0.100, interval=0.010)
        )
        async with monitor_event_loop() as health:
            await asyncio.sleep(0.05)
            began = time.monotonic()
            hold_the_gil_in_c()
            c_call = time.monotonic() - began
            await asyncio.sleep(0.05)
        switch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await switch
    assert c_call > 2 * DEFAULT_BLOCK_THRESHOLD
    assert f"line {c_line} in hold_the_gil_in_c" in path.read_text()  # C thread sees inside
    event = health.watchdog.events()[0]
    if gil_enabled():
        assert event.stalled_for >= c_call  # the watchdog could not run until the call ended
        assert event.capture_seconds < DEFAULT_BLOCK_THRESHOLD
    else:
        # Noticed on time, but sys._current_frames() stops the world, and that waits
        # for the C call to reach a safe point, so the snapshot itself blocks.
        assert event.stalled_for < c_call
        assert event.capture_seconds > DEFAULT_BLOCK_THRESHOLD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_monitor_teardown_does_not_swallow_a_cancel_aimed_at_its_caller() -> None:
    entered = asyncio.Event()

    async def worker() -> None:
        async with monitor_event_loop():
            entered.set()  # no await after this: teardown runs straight to `await canary`

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()  # lands while the worker is awaiting the cancelled canary
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
