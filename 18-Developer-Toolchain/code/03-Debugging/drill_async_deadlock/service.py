"""Target service for the asyncio half of the async deadlock drill.

Each mode is one hang (or its fix). The service installs two live diagnostics before it
starts, the way a production service should:

- SIGUSR1: an event-loop signal handler that prints every task, its await chain from
  asyncio.capture_call_graph() (Python 3.14+), the lock it waits on, and the wait-for
  graph built from lock owners. It runs as a loop callback, so it answers only if the
  loop thread is free.
- SIGUSR2: faulthandler.register(), which dumps the Python stack of every thread from a
  C signal handler, so it answers even when the loop thread is blocked.

Modes:
  lockorder        two tasks take two asyncio.Locks in opposite order (await cycle)
  lockorder-fixed  the same tasks take the locks in one global order
  unawaited        notify(event) is called without await, so the waiter never wakes
  lostwake         a thread resolves a Future with set_result() instead of
                   call_soon_threadsafe(), so the sleeping loop is never woken
  lostwake-fixed   the same producer uses loop.call_soon_threadsafe()
  blocked          a threading.Lock is held across an await, so the loop thread blocks
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import signal
import sys
import threading
import time


class OwnedLock(asyncio.Lock):
    """An asyncio.Lock that remembers which task holds it; asyncio.Lock does not."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.owner: asyncio.Task | None = None

    async def acquire(self) -> bool:
        await super().acquire()
        self.owner = asyncio.current_task()
        return True

    def release(self) -> None:
        self.owner = None
        super().release()


def waited_lock(frames) -> OwnedLock | None:
    """The OwnedLock whose acquire() frame is on this await chain, read from frame locals."""
    for frame in frames:
        candidate = frame.f_locals.get("self")
        if frame.f_code.co_name == "acquire" and isinstance(candidate, OwnedLock):
            return candidate
    return None


def dump_tasks() -> None:
    print("=== SIGUSR1 task dump (ran as an event loop callback) ===")
    edges: dict[str, str] = {}
    for task in sorted(asyncio.all_tasks(), key=lambda t: t.get_name()):
        graph = asyncio.capture_call_graph(task)
        frames = [entry.frame for entry in graph.call_stack]
        chain = " -> ".join(f.f_code.co_qualname for f in frames)
        awaiters = ",".join(g.future.get_name() for g in graph.awaited_by) or "-"
        print(f"task {task.get_name()}: awaited by {awaiters}; stack: {chain}")
        lock = waited_lock(frames)
        if lock is not None:
            owner = lock.owner.get_name() if lock.owner else "nobody"
            print(f"  waits on lock {lock.name}, held by {owner}")
            if lock.owner is not None:
                edges[task.get_name()] = owner
    print("wait-for cycle:", find_cycle(edges) or "none")
    print("=== end dump ===", flush=True)


def find_cycle(edges: dict[str, str]) -> str:
    for start in sorted(edges):
        path, node = [start], edges[start]
        while node in edges and node not in path:
            path.append(node)
            node = edges[node]
        if node == start:
            return " -> ".join([*path, start])
    return ""


async def transfer(first: OwnedLock, second: OwnedLock) -> None:
    async with first:
        await asyncio.sleep(0.05)
        async with second:
            pass


async def lockorder(fixed: bool) -> None:
    a, b = OwnedLock("A"), OwnedLock("B")
    plans = {"transfer-ab": (a, b), "transfer-ba": (b, a)}
    async with asyncio.TaskGroup() as tg:
        for name, pair in plans.items():
            first, second = sorted(pair, key=lambda lk: lk.name) if fixed else pair
            tg.create_task(transfer(first, second), name=name)
    print("transfers completed: 2", flush=True)


async def notify(event: asyncio.Event) -> None:
    event.set()


async def unawaited() -> None:
    event = asyncio.Event()

    async def waiter() -> None:
        try:
            async with asyncio.timeout(0.3):
                await event.wait()
            print("waiter: event received", flush=True)
        except TimeoutError:
            print("waiter: TimeoutError after the 0.3 s guard; the event was never set", flush=True)

    task = asyncio.create_task(waiter(), name="waiter")
    notify(event)  # BUG: the coroutine object is created and discarded, never run
    await task


async def lostwake(fixed: bool) -> None:
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def producer() -> None:
        time.sleep(0.2)  # long enough for the loop to be asleep in select()
        try:
            if fixed:
                loop.call_soon_threadsafe(fut.set_result, 42)
            else:
                fut.set_result(42)  # BUG: not thread-safe, never writes the self-pipe
            print("producer: result delivered", flush=True)
        except RuntimeError as exc:
            print(f"producer: RuntimeError: {exc}", flush=True)

    threading.Thread(target=producer, name="producer").start()
    print("consumer: awaiting the future", flush=True)
    print(f"consumer: woke with {await fut}", flush=True)


async def blocked() -> None:
    sync_lock = threading.Lock()

    async def holder() -> None:
        with sync_lock:
            await asyncio.sleep(0.1)  # BUG: a thread lock held across an await

    async def contender() -> None:
        await asyncio.sleep(0.01)
        with sync_lock:  # blocks the only thread the loop has
            pass

    async with asyncio.TaskGroup() as tg:
        tg.create_task(holder(), name="holder")
        tg.create_task(contender(), name="contender")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode")
    ap.add_argument("--debug", action="store_true", help="asyncio debug mode")
    args = ap.parse_args()
    faulthandler.register(signal.SIGUSR2, file=sys.stderr, all_threads=True)

    async def entry() -> None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, dump_tasks)
        print("ready", flush=True)
        mode = args.mode
        if mode.startswith("lockorder"):
            await lockorder(mode.endswith("-fixed"))
        elif mode == "unawaited":
            await unawaited()
        elif mode.startswith("lostwake"):
            await lostwake(mode.endswith("-fixed"))
        elif mode == "blocked":
            await blocked()
        else:
            raise SystemExit(f"unknown mode {mode}")

    # debug=False would override -X dev and PYTHONASYNCIODEBUG, so pass None unless asked.
    asyncio.run(entry(), debug=True if args.debug else None)


if __name__ == "__main__":
    main()
