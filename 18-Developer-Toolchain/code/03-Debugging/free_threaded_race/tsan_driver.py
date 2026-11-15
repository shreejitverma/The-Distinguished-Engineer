"""Drive the racy extension's counters under one of three thread schedules.

run.sh runs this under ThreadSanitizer (the TSan runtime injected with
DYLD_INSERT_LIBRARIES, the extension built with -fsanitize=thread) and reads
TSan's report from stderr. stdout carries one line: "<counter> <expected> <parked>".

Schedules:
- plain, atomic, mutex: THREADS threads leave a barrier together and bump ITERS times each,
  so the counter is contended. Without the barrier the threads can run one after another,
  the PyMutex is then never contended, and TSan has nothing to report.
- turns_plain, turns_mutex: two threads take strict turns, TURNS increments each,
  so no two increments overlap and the PyMutex is never contended.
- handoff: one thread holds the PyMutex until the other has parked on it, then unlocks.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

import racy

THREADS = 4
# Long enough that four loops cannot all finish inside one scheduler timeslice on a busy host.
ITERS = 250_000
TURNS = 2_000
# How long the holder keeps the mutex after it sees the parked bit, so the waiter is asleep.
SETTLE_US = 200_000


def run_all(targets: list[Callable[[], object]]) -> None:
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def at_once(bump: Callable[[int], None]) -> int:
    barrier = threading.Barrier(THREADS)

    def body() -> None:
        barrier.wait()
        bump(ITERS)

    run_all([body] * THREADS)
    return THREADS * ITERS


def in_turns(locked: bool) -> int:
    run_all([lambda me=me: racy.bump_turns(me, TURNS, locked) for me in (0, 1)])
    return 2 * TURNS


def handoff() -> tuple[int, bool]:
    parked: list[bool] = []
    run_all([lambda: parked.append(racy.hold_then_bump(SETTLE_US)), racy.block_then_bump])
    return 2, parked[0]


def main() -> int:
    variant = sys.argv[1]
    parked = "-"
    if variant in ("plain", "atomic", "mutex"):
        want = at_once(getattr(racy, f"bump_{variant}"))
    elif variant in ("turns_plain", "turns_mutex"):
        want = in_turns(locked=variant == "turns_mutex")
    elif variant == "handoff":
        want, was_parked = handoff()
        parked = "yes" if was_parked else "no"
    else:
        print(f"unknown variant: {variant}", file=sys.stderr)
        return 2
    plain, atomic, mutex = racy.counts()
    counter = {"plain": plain, "atomic": atomic, "turns_plain": plain}.get(variant, mutex)
    print(f"{counter} {want} {parked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
