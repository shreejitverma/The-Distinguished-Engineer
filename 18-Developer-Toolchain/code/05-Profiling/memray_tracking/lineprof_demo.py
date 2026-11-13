"""line_profiler 5.1.0 mechanisms, printed as facts that do not depend on timing noise.

Prints hit counts (deterministic), the bytecode padding line_profiler applies to profiled
functions, the sys.monitoring state while a profile is enabled, and where time is charged.
Exits non-zero if a mechanism stops behaving as the note describes.
"""

import inspect
import sys
import time

from line_profiler import LineProfiler

N = 1000
SLEEP_S = 0.02
SLEEPS = 5


def hot(n):
    total = 0
    for i in range(n):
        total += i
        if i % 100 == 0:
            total += sum(range(50_000))
    return total


def sleeper():
    for _ in range(SLEEPS):
        time.sleep(SLEEP_S)


def fact(k):
    if k <= 1:
        return 1
    return k * fact(k - 1)


def twin_a(x):
    return x + 1


def twin_b(x):
    return x + 1


def check(cond, msg):
    if not cond:
        print(f"CHECK FAILED: {msg}", file=sys.stderr)
        sys.exit(1)


def show_hits(stats, func):
    code = func.__code__
    key = (code.co_filename, code.co_firstlineno, func.__qualname__)
    rows = {lineno: (hits, t) for lineno, hits, t in stats.timings[key]}
    src, first = inspect.getsourcelines(func)
    print(f"  {func.__name__}: line offset, hits, source")
    for off, text in enumerate(src):
        hits = rows.get(first + off, (0, 0))[0]
        print(f"    +{off}  {hits:>5}  {text.rstrip()}")
    return rows, first


def main():
    print("== line_profiler: bytecode padding ==")
    before_a = len(twin_a.__code__.co_code)
    before_b = len(twin_b.__code__.co_code)
    check(twin_a.__code__.co_code == twin_b.__code__.co_code, "twins start with identical bytecode")
    lp = LineProfiler()
    for f in (hot, sleeper, fact, twin_a, twin_b):
        lp.add_function(f)
    pad_a = len(twin_a.__code__.co_code) - before_a
    pad_b = len(twin_b.__code__.co_code) - before_b
    print(f"  twin_a co_code grew by {pad_a} bytes, twin_b by {pad_b} bytes")
    print(f"  twins still identical after add_function: {twin_a.__code__.co_code == twin_b.__code__.co_code}")
    check(pad_a == 2 and pad_b == 4, "first twin padded with one NOP, second with two")

    print("== line_profiler: sys.monitoring state ==")
    mon = sys.monitoring
    print(f"  before enable: tool 2 = {mon.get_tool(mon.PROFILER_ID)!r}")
    lp.enable_by_count()
    events = mon.get_events(mon.PROFILER_ID)
    print(f"  while enabled: tool 2 = {mon.get_tool(mon.PROFILER_ID)!r}")
    print(f"  global LINE events on: {bool(events & mon.events.LINE)}")
    print(f"  global PY_RETURN events on: {bool(events & mon.events.PY_RETURN)}")
    print(f"  sys.gettrace() is None: {sys.gettrace() is None}")
    hot(N)
    sleeper()
    fact(5)
    twin_a(1)
    twin_b(1)
    twin_b(2)
    lp.disable_by_count()
    print(f"  after disable: tool 2 = {mon.get_tool(mon.PROFILER_ID)!r}")

    stats = lp.get_stats()
    print(f"== line_profiler: timer unit {stats.unit:g} s (internal clock) ==")
    print("== line_profiler: hit counts ==")
    rows, first = show_hits(stats, hot)
    check(rows[first + 2][0] == N + 1, "the for line is hit once per iteration plus the exhausting check")
    rows_s, first_s = show_hits(stats, sleeper)
    rows_f, _ = show_hits(stats, fact)
    a_rows, _ = show_hits(stats, twin_a)
    b_rows, _ = show_hits(stats, twin_b)
    check(sum(h for h, _ in a_rows.values()) == 1 and sum(h for h, _ in b_rows.values()) == 2,
          "twin hits are kept apart")

    print("== line_profiler: where time is charged ==")
    sleep_line_s = rows_s[first_s + 2][1] * stats.unit
    func_total_s = sum(t for _, t in rows_s.values()) * stats.unit
    floor = SLEEP_S * SLEEPS
    print(f"  sleep line total >= {floor:g} s: {sleep_line_s >= floor}")
    print(f"  sleep line share of sleeper() > 95%: {sleep_line_s / func_total_s > 0.95}")
    hot_line = max(rows, key=lambda ln: rows[ln][1]) - first
    print(f"  hottest line in hot(): +{hot_line} (the sum(range(50_000)) line is +5)")
    check(sleep_line_s >= floor, "a line is charged the wall time of the calls it makes")


if __name__ == "__main__":
    main()
