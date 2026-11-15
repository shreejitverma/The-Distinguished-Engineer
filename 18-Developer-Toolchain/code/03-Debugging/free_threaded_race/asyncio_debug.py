"""Three asyncio bugs that production mode hides and debug mode reports.

run.sh runs this file twice: with PYTHONASYNCIODEBUG unset and with PYTHONASYNCIODEBUG=1.
1. A coroutine that blocks the loop with time.sleep(0.25).
2. loop.call_soon() called from a non-loop thread instead of call_soon_threadsafe().
3. A coroutine object that is created and never awaited.
The script prints derived facts, never raw reprs, because those hold paths and addresses.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import re
import sys
import threading
import time
import warnings

BLOCK_S = 0.25
SLOW_RE = re.compile(r"^Executing <Task .*coro=<(\w+)\(\).*> took (\d+\.\d+) seconds$")


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def blocker() -> None:
    time.sleep(BLOCK_S)  # the bug: a blocking call inside a coroutine


async def never_awaited() -> None:
    pass


async def main(capture: Capture) -> None:
    loop = asyncio.get_running_loop()
    print(f"PYTHONASYNCIODEBUG={os.environ.get('PYTHONASYNCIODEBUG', '(unset)')}: "
          f"loop.get_debug() = {loop.get_debug()}, slow_callback_duration = {loop.slow_callback_duration}")

    await asyncio.create_task(blocker())
    slow = [m for m in capture.messages if m.startswith("Executing ")]
    print(f"  1 blocking time.sleep({BLOCK_S}): slow-callback warnings = {len(slow)}")
    for m in slow:
        hit = SLOW_RE.match(m)
        if hit is None:
            print(f"    unparsed warning: {m[:60]}")
        else:
            took = float(hit.group(2))
            print(f"    Executing <Task ... coro=<{hit.group(1)}()> ...> took >= {BLOCK_S} s: {took >= BLOCK_S}")

    errors: list[str] = []

    def from_other_thread() -> None:
        try:
            loop.call_soon(lambda: None)
        except RuntimeError as e:
            errors.append(f"RuntimeError: {e}")

    t = threading.Thread(target=from_other_thread)
    t.start()
    t.join()
    print(f"  2 call_soon from another thread: {errors[0] if errors else 'accepted silently'}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        never_awaited()
        gc.collect()
    msgs = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    first = msgs[0].splitlines()[0] if msgs else "(none)"
    origin = any("Coroutine created at" in m for m in msgs)
    print(f"  3 never-awaited coroutine: {first}")
    print(f"    creation traceback attached: {origin}")


if __name__ == "__main__":
    cap = Capture()
    log = logging.getLogger("asyncio")
    log.addHandler(cap)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    asyncio.run(main(cap))
    sys.exit(0)
