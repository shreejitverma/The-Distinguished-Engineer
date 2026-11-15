"""Pre-fork worker topologies: recycling budgets, heartbeats, fork state, and per-worker memory.

Drives the real Gunicorn 26.2.0 and Uvicorn 0.54.0 objects that decide when a worker is
recycled or killed, and real os.fork() children to show what a worker inherits.
"""

from __future__ import annotations

import asyncio
import gc
import importlib
import itertools
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server as UvicornServer
from uvicorn.supervisors.multiprocess import Process as UvicornProcess

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

# Gunicorn ships no type information, so its modules are loaded as untyped values.
_gunicorn_config: Any = importlib.import_module("gunicorn.config")
_gunicorn_worker_base: Any = importlib.import_module("gunicorn.workers.base")
_gunicorn_workertmp: Any = importlib.import_module("gunicorn.workers.workertmp")
_gunicorn_arbiter: Any = importlib.import_module("gunicorn.arbiter")


# -- Recycling budgets: the exact formulas both servers use -------------------------------


def gunicorn_request_budget(max_requests: int, jitter: int, *, seed: int) -> int:
    """Return the max_requests budget a real Gunicorn Worker draws for itself."""
    cfg = _gunicorn_config.Config()
    cfg.set("max_requests", max_requests)
    cfg.set("max_requests_jitter", jitter)
    random.seed(seed)  # Worker.__init__ calls the module-level random.randint
    worker = _gunicorn_worker_base.Worker(1, os.getpid(), [], None, 15.0, cfg, None)
    try:
        return int(worker.max_requests)
    finally:
        worker.tmp.close()


def uvicorn_request_budget(limit: int | None, jitter: int, *, seed: int) -> int | None:
    """Return the limit_max_requests a real Uvicorn Server computes (a cached property)."""
    config = UvicornConfig(
        app="unused:app", limit_max_requests=limit, limit_max_requests_jitter=jitter
    )
    random.seed(seed)
    return UvicornServer(config).limit_max_requests


@dataclass
class RecyclePlan:
    """Global request indices at which each worker slot was recycled."""

    restarts: list[int] = field(default_factory=lambda: list[int]())

    def worst_burst(self, window: int) -> int:
        """Largest number of restarts that fall inside any `window` consecutive requests."""
        ordered = sorted(self.restarts)
        best = 0
        start = 0
        for end, value in enumerate(ordered):
            while value - ordered[start] >= window:
                start += 1
            best = max(best, end - start + 1)
        return best


def simulate_recycling(workers: int, budget: Callable[[], int], total_requests: int) -> RecyclePlan:
    """Dispatch requests round-robin; a worker that reaches its budget is replaced.

    Round-robin is the evenest possible spread, which is the worst case for synchronized
    restarts: with zero jitter every worker crosses its budget in the same few requests.
    """
    budgets = [budget() for _ in range(workers)]
    served = [0] * workers
    plan = RecyclePlan()
    for index in range(total_requests):
        slot = index % workers
        served[slot] += 1
        if served[slot] >= budgets[slot]:
            plan.restarts.append(index)
            served[slot] = 0
            budgets[slot] = budget()
    return plan


# -- Heartbeats: what each supervisor can and cannot see ----------------------------------


def make_heartbeat_file() -> Any:
    """Create the real Gunicorn per-worker heartbeat file (an unlinked temp file)."""
    return _gunicorn_workertmp.WorkerTmp(_gunicorn_config.Config())


def arbiter_murder_signals(timeout: int, silent_for: float, passes: int) -> list[int]:
    """Run the real Arbiter.murder_workers on one worker whose heartbeat is `silent_for` old."""
    tmp = make_heartbeat_file()
    sent: list[int] = []

    def record_kill(pid: int, sig: int) -> None:
        sent.append(sig)

    def ignore_log(*args: object) -> None:
        return None

    try:
        tmp.notify()
        stale = time.monotonic() - silent_for
        os.utime(tmp.fileno(), (stale, stale))  # same clock WorkerTmp.notify writes
        arbiter = _gunicorn_arbiter.Arbiter.__new__(_gunicorn_arbiter.Arbiter)
        arbiter.timeout = timeout
        arbiter.WORKERS = {4242: SimpleNamespace(tmp=tmp, aborted=False)}
        arbiter.log = SimpleNamespace(critical=ignore_log)
        arbiter.kill_worker = record_kill  # stands in for os.kill(pid, sig)
        for _ in range(passes):
            arbiter.murder_workers()
    finally:
        tmp.close()
    return sent


async def loop_heartbeat_gap(block_seconds: float, interval: float) -> float:
    """Largest gap between heartbeats when the heartbeat runs on the event loop.

    This is how Gunicorn's `asgi` worker (and `uvicorn.workers.UvicornWorker`) notify the
    arbiter: from a coroutine, so a blocking call starves the heartbeat.
    """
    tmp = make_heartbeat_file()
    beats: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        while not stop.is_set():
            tmp.notify()
            beats.append(time.monotonic())
            await asyncio.sleep(interval)

    async def blocking_handler() -> None:
        await asyncio.sleep(interval * 2)
        time.sleep(block_seconds)  # noqa: ASYNC251 - the deliberate bug under test
        await asyncio.sleep(interval * 2)
        stop.set()

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat())
            group.create_task(blocking_handler())
    finally:
        tmp.close()
    return max(later - earlier for earlier, later in itertools.pairwise(beats))


@dataclass(frozen=True)
class PingResult:
    answered: bool
    ready: bool
    loop_was_blocked: bool


def uvicorn_ping_while_loop_blocked(block_seconds: float, *, started: bool) -> PingResult:
    """Ask a real Uvicorn supervisor Process for a health ping while its event loop is blocked.

    Uvicorn answers pings from a daemon thread (Process.always_pong), not from the event loop,
    so a worker whose loop is stuck in a blocking call still looks alive to `--workers`.
    """
    process = UvicornProcess(UvicornConfig(app="unused:app"), sockets=[])
    process.server.started = started
    loop_blocked = threading.Event()
    loop_released = threading.Event()

    async def stuck_endpoint() -> None:
        loop_blocked.set()
        time.sleep(block_seconds)  # noqa: ASYNC251 - the deliberate bug under test
        loop_released.set()

    loop_thread = threading.Thread(target=asyncio.run, args=(stuck_endpoint(),))
    pong_thread = threading.Thread(target=process.pong)  # one pong, as always_pong does
    loop_thread.start()
    loop_blocked.wait()
    pong_thread.start()
    try:
        answered = process.ping(timeout=block_seconds / 2)
        still_blocked = not loop_released.is_set()
        pong_thread.join()
        pong_thread = threading.Thread(target=process.pong)
        pong_thread.start()
        ready = process.is_ready(timeout=block_seconds / 2)
    finally:
        pong_thread.join()
        loop_thread.join()
        process.parent_conn.close()
        process.child_conn.close()
    return PingResult(answered=answered, ready=ready, loop_was_blocked=still_blocked)


def uvicorn_worker_start_method() -> str:
    """Uvicorn --workers starts children with multiprocessing 'spawn', never fork."""
    subprocess_module: Any = importlib.import_module("uvicorn._subprocess")
    return str(subprocess_module.spawn.get_start_method())


# -- Fork semantics: what a pre-forked worker inherits ------------------------------------


def run_in_forked_child(task: Callable[[], str]) -> str:
    """Fork, run `task` in the child, and return what it printed through a pipe."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(read_end)
        try:
            os.write(write_end, task().encode())
        finally:
            os._exit(0)
    os.close(write_end)
    chunks: list[bytes] = []
    while chunk := os.read(read_end, 65536):
        chunks.append(chunk)
    os.close(read_end)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise RuntimeError(f"forked child exited with status {status}")
    return b"".join(chunks).decode()


def frozen_object_count() -> tuple[int, int]:
    """Freeze, count the permanent generation, unfreeze, count again."""
    gc.freeze()
    frozen = gc.get_freeze_count()
    gc.unfreeze()
    return frozen, gc.get_freeze_count()


def refcount_writes(target: object, references: int) -> int:
    """How much the refcount field of `target` changes when `references` new references exist.

    Every change is a write to the page that holds the object, which is what breaks
    copy-on-write sharing; immortal objects (PEP 683, CPython 3.12+) report no change.
    """
    before = sys.getrefcount(target)
    holders = [target] * references
    after = sys.getrefcount(target)
    del holders
    return after - before


# -- Per-worker memory: what diverges when an app runs in N processes ---------------------


def make_counter_app(store: dict[str, int] | None = None, limit: int = 5) -> Starlette:
    """One worker's copy of an app with a hit counter and an in-memory fixed-window limiter.

    `store=None` models module-level state (private to the process); passing one shared dict
    models an external store such as Redis that every worker reaches.
    """
    state: dict[str, int] = store if store is not None else {}
    worker_id = f"w{id(state) % 100000}"

    async def hit(request: Request) -> JSONResponse:
        client = request.headers.get("x-client", "anon")
        key = f"rate:{client}"
        state[key] = state.get(key, 0) + 1
        if state[key] > limit:
            return JSONResponse({"error": "rate limited"}, status_code=429)
        state["hits"] = state.get("hits", 0) + 1
        return JSONResponse({"worker": worker_id, "hits": state["hits"]})

    return Starlette(routes=[Route("/hit", hit)])


class RoundRobinWorkers:
    """ASGI stand-in for N worker processes behind one listener or Service."""

    def __init__(self, workers: list[Starlette]) -> None:
        self.workers = workers
        self.next_index = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        worker = self.workers[self.next_index % len(self.workers)]
        self.next_index += 1
        await worker(scope, receive, send)
