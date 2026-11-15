"""A lock held by another thread at fork() time is held forever in the child.

A helper thread takes a module-level threading.Lock and keeps it.
The main thread then creates children four ways and each child tries to take the lock
with a timeout, so the lab reports the deadlock instead of hanging.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import re
import sys
import threading
import warnings

LOCK = threading.Lock()
TIMEOUT_S = 2.0
CHILD_TIMED_OUT = 3


def child_try_lock(q: mp.Queue) -> None:
    q.put(LOCK.acquire(timeout=TIMEOUT_S))


def describe(caught: list[warnings.WarningMessage]) -> str:
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    if not dep:
        return "no DeprecationWarning"
    msg = re.sub(r"pid=\d+", "pid=N", str(dep[0].message))
    return f"DeprecationWarning: {msg}"


def raw_fork() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pid = os.fork()
        if pid == 0:
            os._exit(0 if LOCK.acquire(timeout=TIMEOUT_S) else CHILD_TIMED_OUT)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    got = "acquired" if code == 0 else f"timed out after {TIMEOUT_S} s (exit {code})"
    print(f"  os.fork():              child lock.acquire {got}")
    print(f"                          parent saw {describe(caught)}")


def via_context(method: str) -> None:
    ctx = mp.get_context(method)
    q = ctx.Queue()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        p = ctx.Process(target=child_try_lock, args=(q,))
        p.start()
        ok = q.get(timeout=30)
        p.join()
    got = "acquired" if ok else f"timed out after {TIMEOUT_S} s"
    print(f"  multiprocessing {method + ':':<11} child lock.acquire {got}; parent saw {describe(caught)}")


def main() -> int:
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with LOCK:
            held.set()
            release.wait()

    t = threading.Thread(target=holder, name="holder")
    t.start()
    held.wait()
    print(f"parent: {threading.active_count()} threads, LOCK held by thread 'holder'; "
          f"default start method = {mp.get_start_method()}")
    try:
        raw_fork()
        for method in ("spawn", "forkserver", "fork"):
            via_context(method)
    finally:
        release.set()
        t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
