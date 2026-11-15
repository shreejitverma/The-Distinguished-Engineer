"""Lost updates in pure Python, with the GIL on and off, in one interpreter binary.

run.sh runs this file twice on the same free-threaded CPython: once with
PYTHON_GIL=1 and once with PYTHON_GIL=0, so one binary serves both sides.
Every workload is a read-modify-write of shared state from THREADS threads.
A trial reports how many increments were lost; a workload prints "yes" at the
first trial that loses any, or "none in N trials" if all N trials are exact.

With the GIL on, a thread can lose the GIL only at an eval-breaker check, and
the default 5 ms switch interval requests a handoff so rarely that a short run
may never land one inside the window. `--switch-interval 1e-6` makes the
interpreter request a handoff about every microsecond, which turns
"sometimes" into "every trial" without changing what the code allows.
"""

from __future__ import annotations

import argparse
import dis
import os
import sys
import threading
from collections.abc import Callable

THREADS = 8
ITERS = 200_000
TRIALS = 5


class Box:
    def __init__(self) -> None:
        self.n = 0


def touch() -> None:
    """A pure-Python call: its RESUME is an eval-breaker check point."""


def inc_plain(box: Box, lock: threading.Lock) -> None:
    for _ in range(ITERS):
        box.n += 1


def inc_with_call(box: Box, lock: threading.Lock) -> None:
    for _ in range(ITERS):
        v = box.n
        touch()
        box.n = v + 1


def inc_locked(box: Box, lock: threading.Lock) -> None:
    for _ in range(ITERS):
        with lock:
            box.n += 1


def run_threads(target: Callable[..., None], *args: object) -> None:
    barrier = threading.Barrier(THREADS)

    def body() -> None:
        barrier.wait()
        target(*args)

    threads = [threading.Thread(target=body) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def lost_in_trial(work: Callable[[Box, threading.Lock], None]) -> int:
    box, lock = Box(), threading.Lock()
    run_threads(work, box, lock)
    return THREADS * ITERS - box.n


def verdict(work: Callable[[Box, threading.Lock], None], trials: int) -> str:
    for _ in range(trials):
        if lost_in_trial(work) > 0:
            return "yes"
    return f"none in {trials} trials"


def appends() -> str:
    items: list[int] = []

    def work() -> None:
        append = items.append
        for i in range(ITERS):
            append(i)

    run_threads(work)
    return f"{len(items)} of {THREADS * ITERS}"


def window(work: Callable[..., None]) -> str:
    """Opcodes from the load of box.n to the store back to box.n."""
    ops = [i.opname for i in dis.get_instructions(work)]
    start = ops.index("LOAD_ATTR")
    end = ops.index("STORE_ATTR")
    return " ".join(ops[start:end + 1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--switch-interval", type=float, default=None)
    ap.add_argument("--show-bytecode", action="store_true")
    args = ap.parse_args()
    if args.show_bytecode:
        print("Opcodes between the read and the write of box.n (dis, unspecialized names):")
        print(f"  A  {window(inc_plain)}")
        print(f"  B  {window(inc_with_call)}")
        return 0
    if args.switch_interval is not None:
        sys.setswitchinterval(args.switch_interval)
    gil = sys._is_gil_enabled()
    print(f"PYTHON_GIL={os.environ.get('PYTHON_GIL', '(unset)')}: "
          f"sys._is_gil_enabled() = {gil}, switch interval {sys.getswitchinterval()} s, "
          f"{THREADS} threads x {ITERS} iterations")
    print(f"  A  box.n += 1                         lost updates: {verdict(inc_plain, TRIALS)}")
    print(f"  B  v = box.n; touch(); box.n = v + 1  lost updates: {verdict(inc_with_call, TRIALS)}")
    print(f"  C  with lock: box.n += 1              lost updates: {verdict(inc_locked, TRIALS)}")
    print(f"  D  items.append(i)                    items kept: {appends()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
