"""Reduce Google Benchmark JSON output to facts that do not change from run to run."""

from __future__ import annotations

import json
import sys


def main(path: str, compiler: str) -> int:
    with open(path, encoding="ascii") as f:
        doc = json.load(f)
    runs = {b["name"]: b for b in doc["benchmarks"] if b.get("run_type") == "iteration"}
    big_o = {
        b["run_name"]: b["big_o"] for b in doc["benchmarks"] if b.get("aggregate_name") == "BigO"
    }
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(f"  check {what:<58} {'ok' if ok else 'FAILED'}")
        failures += 0 if ok else 1

    print(f"[5] Google Benchmark, time-based ({compiler})")
    # cpu_time is per iteration, in the family's time unit (ns by default).
    discarded = runs["BM_SumDiscarded"]["cpu_time"]
    sunk = runs["BM_SumDoNotOptimize"]["cpu_time"]
    print(f"  BM_SumDiscarded under 2 ns per iteration: {'yes' if discarded < 2.0 else 'no'}")
    print(f"  BM_SumDoNotOptimize at least 50x slower : {'yes' if sunk > 50.0 * discarded else 'no'}")
    check(sunk > 50.0 * discarded, "DoNotOptimize keeps the reduction under the real library")
    # On time, N lgN and N are hard to separate over 64x of range; the exact winner is noise.
    sort_fit = big_o.get("BM_Sort", "missing")
    print(f"  BM_Sort    BigO is NlgN or N: {'yes' if sort_fit in ('NlgN', 'N') else 'no: ' + sort_fit}")
    print(f"  BM_Series  BigO: {big_o.get('BM_Series', 'missing')}")
    check(sort_fit in ("NlgN", "N"), "BM_Sort fits NlgN or N (time-based fit)")
    expected_series = "(1)" if compiler == "clang" else "N"
    check(big_o.get("BM_Series") == expected_series, f"BM_Series fits {expected_series} under {compiler}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
