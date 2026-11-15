"""Workloads for a ThreadSanitizer-instrumented free-threaded CPython.

Usage: tsan_cpython.py attr|item|iter

attr  box.n += 1 from 4 threads (Python-level lost update on an attribute)
item  d["k"] += 1 from 4 threads (Python-level lost update on a dict item)
iter  4 threads drain one shared range iterator (C-level race in rangeiter_next)

Each prints one line of derived facts, never raw counts, so the output is stable.
"""

import sys
import threading

THREADS = 4
ITERS = 100_000


class Box:
    def __init__(self) -> None:
        self.n = 0


def lost_update(kind: str) -> None:
    box = Box()
    d = {"k": 0}
    barrier = threading.Barrier(THREADS)

    def attr() -> None:
        barrier.wait()
        for _ in range(ITERS):
            box.n += 1

    def item() -> None:
        barrier.wait()
        for _ in range(ITERS):
            d["k"] += 1

    target = attr if kind == "attr" else item
    threads = [threading.Thread(target=target) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = box.n if kind == "attr" else d["k"]
    want = THREADS * ITERS
    print(f"gil={sys._is_gil_enabled()} lost updates: {'yes' if got < want else 'no'}")


def shared_iterator() -> None:
    total = THREADS * ITERS
    it = iter(range(total))
    barrier = threading.Barrier(THREADS)
    seen = [0] * THREADS

    def drain(i: int) -> None:
        barrier.wait()
        for _ in it:
            seen[i] += 1

    threads = [threading.Thread(target=drain, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = sum(seen)
    verdict = "duplicates" if got > total else "missing items" if got < total else "exact"
    print(f"gil={sys._is_gil_enabled()} items consumed vs {total}: {verdict}")


if __name__ == "__main__":
    if sys.argv[1] == "iter":
        shared_iterator()
    else:
        lost_update(sys.argv[1])
