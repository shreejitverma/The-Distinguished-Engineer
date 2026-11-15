"""Detecting event-loop blocking: a loop-lag canary, a stack-sampling watchdog,
debug-mode slow-callback capture, and a faulthandler dead man's switch.

The canary task sleeps ``interval`` seconds and records how late it woke up.
The watchdog is an OS thread that reads the canary's heartbeat; when the heartbeat
is older than ``threshold`` it snapshots the event-loop thread's Python stack with
``sys._current_frames()``, so the report names the line that is blocking, while it
is still blocking.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import logging
import statistics
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence

    from starlette.requests import Request

DEFAULT_INTERVAL = 0.010  # canary period, the same 10 ms as Node.js monitorEventLoopDelay
DEFAULT_BLOCK_THRESHOLD = 0.050  # report once the heartbeat is 50 ms stale
DEFAULT_POLL_INTERVAL = 0.005  # how often the watchdog thread looks at the heartbeat
BLOCK_SECONDS = 0.200  # how long the demo endpoints block

_NS = 1_000_000_000


def corrected_lag_samples(lag: float, interval: float) -> list[float]:
    """Return ``lag`` plus the samples a late canary failed to take.

    A canary due every ``interval`` that wakes ``lag`` late missed the wake-ups in
    between; each would have seen ``interval`` less lag than the next.  This is
    HdrHistogram's at-recording coordinated-omission correction
    (``recordValueWithExpectedInterval``), computed in integer nanoseconds so the
    count of synthetic samples does not depend on float rounding.
    """
    if interval <= 0:
        raise ValueError("interval must be positive")
    lag_ns = round(lag * _NS)
    interval_ns = round(interval * _NS)
    samples = [lag_ns]
    missing = lag_ns - interval_ns
    while missing >= interval_ns:
        samples.append(missing)
        missing -= interval_ns
    return [s / _NS for s in samples]


def percentile(samples: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile; ``pct`` in (0, 100]."""
    if not samples:
        raise ValueError("no samples")
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100]")
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil(n * pct / 100)
    return ordered[int(rank) - 1]


class LoopLagMonitor:
    """Canary task: sleep ``interval``, record how late the wake-up was, beat."""

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL,
        *,
        max_samples: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.interval = interval
        self._clock = clock
        self._lock = threading.Lock()  # the heartbeat is read from the watchdog thread
        self._last_beat = clock()
        self.raw: deque[float] = deque(maxlen=max_samples)
        self.corrected: deque[float] = deque(maxlen=max_samples)

    def last_beat(self) -> float:
        with self._lock:
            return self._last_beat

    def _beat(self, now: float) -> None:
        with self._lock:
            self._last_beat = now

    async def run(self) -> None:
        self._beat(self._clock())
        while True:
            due = self._clock() + self.interval
            await asyncio.sleep(self.interval)
            woke = self._clock()
            lag = max(0.0, woke - due)
            self.raw.append(lag)
            self.corrected.extend(corrected_lag_samples(lag, self.interval))
            self._beat(woke)


@dataclass(frozen=True, slots=True)
class StackFrame:
    filename: str
    lineno: int
    function: str


@dataclass(frozen=True, slots=True)
class BlockingEvent:
    stalled_for: float  # heartbeat age when the capture started, in seconds
    stack: tuple[StackFrame, ...]  # outermost first; the blocking line is last
    capture_seconds: float = 0.0  # how long sys._current_frames() and the walk took

    @property
    def innermost(self) -> StackFrame:
        return self.stack[-1]

    @property
    def parked_in_selector(self) -> bool:
        """The loop thread was inside ``selector.select()``: it was idle and the OS
        woke it late, so no Python code on the loop is to blame.  Only detectable on
        the pure-Python selector loop; uvloop polls inside C with no Python frame."""
        if not self.stack:
            return False
        top = self.stack[-1]
        return top.function == "select" and top.filename.endswith("selectors.py")


def capture_thread_stack(thread_id: int) -> tuple[StackFrame, ...]:
    """Snapshot another thread's Python stack without its cooperation."""
    frame = sys._current_frames().get(thread_id)  # pyright: ignore[reportPrivateUsage]
    if frame is None:
        return ()
    try:
        summary = traceback.extract_stack(frame)
    finally:
        del frame  # do not keep the blocked thread's frames alive
    return tuple(StackFrame(f.filename, f.lineno or 0, f.name) for f in summary)


class StackSamplingWatchdog:
    """OS thread that captures the loop thread's stack once per stall episode."""

    def __init__(
        self,
        monitor: LoopLagMonitor,
        *,
        loop_thread_id: int,
        threshold: float = DEFAULT_BLOCK_THRESHOLD,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[BlockingEvent], None] | None = None,
    ) -> None:
        self._monitor = monitor
        self._loop_thread_id = loop_thread_id
        self._threshold = threshold
        self._poll_interval = poll_interval
        self._clock = clock
        self._on_event = on_event  # runs in the watchdog thread, never on the loop
        self._events: list[BlockingEvent] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loop-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def events(self) -> list[BlockingEvent]:
        with self._lock:
            return list(self._events)

    def _run(self) -> None:
        reported_beat: float | None = None
        while not self._stop.wait(self._poll_interval):
            beat = self._monitor.last_beat()
            stalled = self._clock() - beat
            if stalled < self._threshold or beat == reported_beat:
                continue
            reported_beat = beat  # one capture per stall; the next beat re-arms
            began = self._clock()
            stack = capture_thread_stack(self._loop_thread_id)
            event = BlockingEvent(stalled, stack, self._clock() - began)
            with self._lock:
                self._events.append(event)
            if self._on_event is not None:
                self._on_event(event)


@dataclass(frozen=True, slots=True)
class LoopHealth:
    monitor: LoopLagMonitor
    watchdog: StackSamplingWatchdog

    def max_lag(self) -> float:
        return max(self.monitor.raw, default=0.0)

    def blocking_events(self) -> list[BlockingEvent]:
        """Stalls caused by code running on the loop thread."""
        return [e for e in self.watchdog.events() if not e.parked_in_selector]

    def wakeup_delays(self) -> list[BlockingEvent]:
        """Stalls where the loop sat in ``select()`` past its timeout."""
        return [e for e in self.watchdog.events() if e.parked_in_selector]


@contextlib.asynccontextmanager
async def monitor_event_loop(
    *,
    interval: float = DEFAULT_INTERVAL,
    block_threshold: float = DEFAULT_BLOCK_THRESHOLD,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> AsyncGenerator[LoopHealth]:
    """Run the canary on the current loop and the watchdog on its own thread."""
    monitor = LoopLagMonitor(interval)
    watchdog = StackSamplingWatchdog(
        monitor,
        loop_thread_id=threading.get_ident(),
        threshold=block_threshold,
        poll_interval=poll_interval,
    )
    canary = asyncio.create_task(monitor.run(), name="loop-lag-canary")
    watchdog.start()
    try:
        yield LoopHealth(monitor, watchdog)
    finally:
        watchdog.stop()  # stop first: a cancelled canary must not look like a stall
        canary.cancel()
        try:
            await canary
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # we are being cancelled too; do not swallow it


class _MessageList(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__(logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record.getMessage())


def capture_slow_callback_warnings(
    main: Callable[[], Coroutine[Any, Any, None]],
    *,
    debug: bool,
    slow_callback_duration: float,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
) -> list[str]:
    """Run ``main()`` on a fresh loop and return its ``Executing ... took`` warnings."""
    messages: list[str] = []
    handler = _MessageList(messages)
    logger = logging.getLogger("asyncio")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        with asyncio.Runner(debug=debug, loop_factory=loop_factory) as runner:
            runner.get_loop().slow_callback_duration = slow_callback_duration
            runner.run(main())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    return [m for m in messages if m.startswith("Executing ")]


async def step_that_blocks(duration: float) -> None:
    time.sleep(duration)  # noqa: ASYNC251 - the blocking call under test
    await asyncio.sleep(0)  # the task is parked here when the slow step is logged


def hold_the_gil_in_c(n: int = 50_000_000) -> None:
    """A long C call that never reaches an eval-breaker check (builtin ``sum``)."""
    sum(range(n))


async def faulthandler_dead_mans_switch(
    file: IO[str], *, dump_after: float, interval: float
) -> None:
    """Re-arm faulthandler's C watchdog thread every ``interval`` seconds.

    If the loop stalls longer than ``dump_after`` the re-arm does not happen and the
    C thread dumps every Python thread's stack to ``file`` without needing the GIL.
    """
    try:
        while True:
            faulthandler.dump_traceback_later(dump_after, repeat=False, file=file, exit=False)
            await asyncio.sleep(interval)
    finally:
        faulthandler.cancel_dump_traceback_later()


async def blocking_endpoint(request: Request) -> PlainTextResponse:
    time.sleep(BLOCK_SECONDS)  # noqa: ASYNC251 - the bug: a sync call on the loop thread
    return PlainTextResponse("blocked the loop")


def offloaded_endpoint(request: Request) -> PlainTextResponse:
    time.sleep(BLOCK_SECONDS)  # Starlette runs a sync endpoint in anyio's thread pool
    return PlainTextResponse("ran in a worker thread")


async def fast_endpoint(request: Request) -> PlainTextResponse:
    return PlainTextResponse("fast")


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/block", blocking_endpoint),
            Route("/offload", offloaded_endpoint),
            Route("/fast", fast_endpoint),
        ]
    )


def _idle_lag_ms(loop_factory: Callable[[], asyncio.AbstractEventLoop] | None) -> str:
    async def sample() -> list[float]:
        monitor = LoopLagMonitor(DEFAULT_INTERVAL)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return list(monitor.raw)

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        lags = runner.run(sample())
    ms = [x * 1000 for x in lags]
    return (
        f"n={len(ms)} p50={percentile(ms, 50):.3f}ms p99={percentile(ms, 99):.3f}ms "
        f"max={max(ms):.3f}ms"
    )


def _callbacks_per_second(debug: bool, n: int = 200_000) -> float:
    async def run() -> float:
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        remaining = [n]

        def tick() -> None:
            remaining[0] -= 1
            if remaining[0] == 0:
                done.set_result(None)
            else:
                loop.call_soon(tick)

        start = time.perf_counter()
        loop.call_soon(tick)
        await done
        return n / (time.perf_counter() - start)

    with asyncio.Runner(debug=debug) as runner:
        return runner.run(run())


def _per_call_us(fn: Callable[[], object], n: int) -> float:
    start = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - start) / n * 1e6


def measure_overheads() -> list[str]:
    """One-shot measurements quoted in the note's Numbers and limits table."""
    lines = [f"python {sys.version.split()[0]} gil_enabled={gil_enabled()}"]
    lines.append(f"idle lag asyncio: {_idle_lag_ms(None)}")
    with contextlib.suppress(ImportError):
        import uvloop

        lines.append(f"idle lag uvloop: {_idle_lag_ms(uvloop.new_event_loop)}")
    off = statistics.median(_callbacks_per_second(False) for _ in range(3))
    on = statistics.median(_callbacks_per_second(True) for _ in range(3))
    lines.append(f"call_soon chain: debug off {off:,.0f}/s, debug on {on:,.0f}/s")
    me = threading.get_ident()
    lines.append(
        f"capture_thread_stack: {_per_call_us(lambda: capture_thread_stack(me), 5000):.1f}us"
    )
    with open("/dev/null", "w") as sink:
        arm = _per_call_us(lambda: faulthandler.dump_traceback_later(60, file=sink), 20000)
        faulthandler.cancel_dump_traceback_later()
    lines.append(f"dump_traceback_later re-arm: {arm:.2f}us")
    return lines


def gil_enabled() -> bool:
    probe: Callable[[], bool] | None = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else probe()


if __name__ == "__main__":
    for line in measure_overheads():
        print(line)
