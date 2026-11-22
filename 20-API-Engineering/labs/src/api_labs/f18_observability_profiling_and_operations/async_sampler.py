"""Wall-clock sampling of asyncio tasks: the await chain of suspended tasks, in-loop vs thread."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from collections import Counter
from typing import Any, cast


def await_chain(task: asyncio.Task[Any]) -> tuple[str, ...]:
    """Outermost-first qualified names of the coroutines a task is suspended inside.

    The chain ends at the object the innermost coroutine awaits, shown as ``<TypeName>``;
    for a C-implemented ``Future`` that object is a ``FutureIter``, which hides the future.
    """
    names: list[str] = []
    awaited: object = task.get_coro()
    while awaited is not None:
        if isinstance(awaited, types.CoroutineType):
            names.append(awaited.cr_code.co_qualname)
            awaited = awaited.cr_await
        elif isinstance(awaited, types.GeneratorType):
            names.append(awaited.gi_code.co_qualname)
            awaited = cast("object", awaited.gi_yieldfrom)
        else:
            names.append(f"<{type(awaited).__name__}>")
            break
    return tuple(names)


async def sample_in_loop(
    interval_s: float, duration_s: float
) -> tuple[Counter[tuple[str, ...]], float]:
    """Sample every other task from inside the loop; return counts and the largest gap seen."""
    counts: Counter[tuple[str, ...]] = Counter()
    me = asyncio.current_task()
    deadline = time.perf_counter() + duration_s
    last = time.perf_counter()
    largest_gap = 0.0
    while time.perf_counter() < deadline:
        now = time.perf_counter()
        largest_gap = max(largest_gap, now - last)
        last = now
        for task in asyncio.all_tasks():
            if task is not me and not task.done():
                counts[await_chain(task)] += 1
        await asyncio.sleep(interval_s)
    return counts, largest_gap


class ThreadSampler:
    """Sample the loop thread's innermost Python frame from a separate thread.

    Unlike ``sample_in_loop`` this sees a loop that is blocked, because it does not need the
    loop to schedule it.
    """

    def __init__(self, target_thread_id: int, interval_s: float = 0.002) -> None:
        self._target = target_thread_id
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="thread-sampler", daemon=True)
        self.counts: Counter[str] = Counter()

    def __enter__(self) -> ThreadSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            frame = sys._current_frames().get(self._target)  # pyright: ignore[reportPrivateUsage]
            if frame is not None:
                code = frame.f_code
                self.counts[f"{code.co_qualname}"] += 1
            self._stop.wait(self._interval_s)
