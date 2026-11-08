#!/usr/bin/env python3
"""Closed-form branch counts, plus a real timing of the lab's own regexes in CPython `re`.

The table is arithmetic, not a measurement.
The two booleans below it are measured: they time `filter_stream.PATTERN_VULN` and `PATTERN_LINEAR`,
so they fail if the nested quantifier is removed or if the linear pattern is not linear.
Only booleans are printed because timings differ from machine to machine.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filter_stream import PATTERN_LINEAR, PATTERN_VULN  # noqa: E402

PREFIX = "[2026-11-07T08:00:06Z] GET path=/api/v1/"
SUFFIX = "! status=403 latency=99ms"
GROWTH_BAND = (2.5, 6.0)  # expected about 4 per two extra characters (2^2)
LINEAR_BAND = (1.3, 3.2)  # expected about 2 when the input doubles


def closed_form_branches(n: int) -> int:
    # A run of n route characters with no slash splits into 2^(n-1) ordered groupings.
    return 1 << (n - 1)


def linear_steps(n: int) -> int:
    # One pass over the n characters plus the boundary check.
    return n + 1


def best_time(pattern, n: int, repeats: int) -> float:
    line = PREFIX + "a" * n + SUFFIX
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        assert pattern.match(line) is None  # the trailing "!" makes every line fail
        best = min(best, time.perf_counter() - t0)
    return best


def main() -> None:
    print("CLOSED-FORM BRANCH COUNTS (arithmetic, not measured):")
    print("N_CHARS | NESTED_BRANCHES | LINEAR_STEPS | RATIO")
    for n in [5, 10, 15, 20]:
        nested = closed_form_branches(n)
        linear = linear_steps(n)
        print(f"{n:7d} | {nested:15d} | {linear:12d} | {nested / linear:10.1f}x")

    print("")
    print("MEASURED ON THE LAB PATTERNS (CPython re, best of 3, N = 16, 18, 20, 22):")
    sizes = [16, 18, 20, 22]
    times = [best_time(PATTERN_VULN, n, 3) for n in sizes]
    ratios = [b / a for a, b in zip(times, times[1:])]
    growth_ok = all(GROWTH_BAND[0] <= r <= GROWTH_BAND[1] for r in ratios)
    print(f"VULN_PATTERN_TIME_GROWS_4X_PER_2_CHARS: {growth_ok}")

    lin_a = best_time(PATTERN_LINEAR, 100_000, 5)
    lin_b = best_time(PATTERN_LINEAR, 200_000, 5)
    linear_ok = LINEAR_BAND[0] <= lin_b / lin_a <= LINEAR_BAND[1]
    print(f"LINEAR_PATTERN_TIME_DOUBLES_WITH_INPUT: {linear_ok}")


if __name__ == "__main__":
    main()
