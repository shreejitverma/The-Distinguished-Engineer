#!/usr/bin/env python3
"""Measure the recscore variants and print facts that do not vary run to run.

Timed phases print thresholds (yes/no), never raw timings.  Counted phases
print exact counts, because the counts are fixed by the code path.

Usage: drill.py <build-dir>
"""

from __future__ import annotations

import ctypes
import importlib
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from sample_stacks import summarize
from service import RECORDS_PER_BATCH, make_payload

HERE = Path(__file__).resolve().parent
VARIANTS = ("hold", "base", "regr", "gilfix", "fixed")
TIMED = ("base", "regr", "gilfix", "fixed")
THREADS = 8
WINDOW_S = 0.4
REPEATS = 5


@dataclass
class Run:
    """One timed window: throughput plus the operating system's view of it."""

    records_per_s: float
    cores_busy: float  # process CPU seconds per wall second
    switches_per_1000: float  # context switches per 1000 records
    system_share: float  # system CPU time as a share of all CPU time


def yn(flag: bool) -> str:
    return "yes" if flag else "no"


def timed_run(mod, threads: int, data: bytes) -> Run:
    """Run `threads` workers for WINDOW_S seconds and measure the window."""
    barrier = threading.Barrier(threads + 1)
    stats: dict = {}
    done = [0] * threads
    deadline = [0.0]

    def worker(slot: int) -> None:
        barrier.wait()
        batches = 0
        while time.monotonic() < deadline[0]:
            mod.score_batch(data, stats)
            batches += 1
        done[slot] = batches

    pool = [threading.Thread(target=worker, args=(k,)) for k in range(threads)]
    for t in pool:
        t.start()
    usage0 = resource.getrusage(resource.RUSAGE_SELF)
    deadline[0] = time.monotonic() + WINDOW_S
    wall0 = time.perf_counter()
    barrier.wait()
    for t in pool:
        t.join()
    wall = time.perf_counter() - wall0
    usage1 = resource.getrusage(resource.RUSAGE_SELF)
    records = sum(done) * RECORDS_PER_BATCH
    user = usage1.ru_utime - usage0.ru_utime
    system = usage1.ru_stime - usage0.ru_stime
    # Linux counts a blocked thread under ru_nvcsw; macOS reports ru_nvcsw as 0
    # and counts every switch under ru_nivcsw.  The sum is comparable on both.
    switches = (usage1.ru_nvcsw - usage0.ru_nvcsw) + (
        usage1.ru_nivcsw - usage0.ru_nivcsw
    )
    cpu = user + system
    return Run(
        records_per_s=records / wall,
        cores_busy=cpu / wall,
        switches_per_1000=1000.0 * switches / max(records, 1),
        system_share=system / cpu if cpu > 0 else 0.0,
    )


def measure(mods: dict, data: bytes) -> dict:
    """Best window per (variant, threads), with the variants interleaved.

    The host may be busy, so each configuration gets REPEATS windows and keeps
    the fastest, and the variants take turns so that a slow minute hits all of
    them alike.
    """
    best: dict = {}
    for _ in range(REPEATS):
        for variant in TIMED:
            for threads in (1, THREADS):
                run = timed_run(mods[variant], threads, data)
                key = (variant, threads)
                if key not in best or run.records_per_s > best[key].records_per_s:
                    best[key] = run
    if os.environ.get("DRILL_RAW") == "1":
        # Raw numbers vary by machine and by run, so they go to stderr only.
        for (variant, threads), run in best.items():
            print(
                f"raw {variant:<6} threads={threads} records/s={run.records_per_s / 1e6:.2f}M "
                f"cores={run.cores_busy:.2f} switches/1000={run.switches_per_1000:.1f} "
                f"system={run.system_share:.0%}",
                file=sys.stderr,
            )
    return best


def phase_symptom(runs: dict) -> None:
    print("== Phase 1: symptom - throughput against thread count ==")
    base1, baseN = runs["base", 1].records_per_s, runs["base", THREADS].records_per_s
    regr1, regrN = runs["regr", 1].records_per_s, runs["regr", THREADS].records_per_s
    print(
        f"release 1.4 (base): {THREADS} threads at least 2.0x the throughput of 1 thread: {yn(baseN >= 2.0 * base1)}"
    )
    print(
        f"release 1.5 (regr): {THREADS} threads at least 1.5x the throughput of 1 thread: {yn(regrN >= 1.5 * regr1)}"
    )
    print(
        f"regr against base, 1 thread: at least 3.0x slower: {yn(base1 >= 3.0 * regr1)}"
    )
    print(
        f"regr against base, {THREADS} threads: at least 5.0x slower: {yn(baseN >= 5.0 * regrN)}"
    )
    print()
    print(f"== Phase 2: operating system view, {THREADS} threads ==")
    for variant in ("base", "regr"):
        r = runs[variant, THREADS]
        print(f"{variant}: at least 2.5 cores busy: {yn(r.cores_busy >= 2.5)}")
        print(
            f"{variant}: at least 20 context switches per 1000 records: {yn(r.switches_per_1000 >= 20.0)}"
        )
        print(
            f"{variant}: system time at least 10% of CPU time: {yn(r.system_share >= 0.10)}"
        )
    print()


def read_probe(mod) -> tuple[int, int, int, int]:
    """Read and reset (malloc, free, PyGILState_Ensure, PyEval_SaveThread) of a probed build."""
    lib = ctypes.CDLL(mod.__file__)
    out = (ctypes.c_long * 4)()
    lib.recscore_probe_read(out)
    return out[0], out[1], out[2], out[3]


def run_batches(mod, data: bytes, stats: dict, batches: int) -> None:
    for _ in range(batches):
        mod.score_batch(data, stats)


def heartbeat(stop: threading.Event, gaps: list[float]) -> None:
    """Sleep 1 ms, then record how long it took to get running again."""
    last = time.perf_counter()
    while not stop.is_set():
        time.sleep(0.001)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now


def phase_probe(build_dir: str, data: bytes) -> None:
    batches = 25
    records = THREADS * batches * RECORDS_PER_BATCH
    print(
        f"== Phase 3: call counts in the probed build, {THREADS} threads, {records} records =="
    )
    print(
        f"{'variant':<8}{'score_batch':>12}{'SaveThread':>12}{'GILState_Ensure':>17}{'Ensure/record':>15}"
        f"{'malloc':>9}{'free':>9}{'malloc/record':>15}"
    )
    for variant in VARIANTS:
        mod = importlib.import_module(f"recscore_{variant}_probe")
        read_probe(mod)
        stats: dict = {}
        pool = [
            threading.Thread(target=run_batches, args=(mod, data, stats, batches))
            for _ in range(THREADS)
        ]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        n_malloc, n_free, n_ensure, n_save = read_probe(mod)
        print(
            f"{variant:<8}{THREADS * batches:>12}{n_save:>12}{n_ensure:>17}{n_ensure / records:>15.2f}"
            f"{n_malloc:>9}{n_free:>9}{n_malloc / records:>15.2f}"
        )
    print()


def phase_allocators(data: bytes) -> None:
    import allocspy

    batches = 50
    records = batches * RECORDS_PER_BATCH
    print(f"== Phase 4: CPython allocator domains, 1 thread, {records} records ==")
    print(
        f"{'variant':<8}{'obj malloc/record':>19}{'obj realloc/record':>20}{'obj free/record':>17}"
        f"{'mem calls/record':>18}{'raw calls/record':>18}"
    )
    for variant in VARIANTS:
        mod = importlib.import_module(f"recscore_{variant}")
        stats: dict = {}
        mod.score_batch(data, stats)  # warm up: create the 8 dict keys before counting
        allocspy.start()
        for _ in range(batches):
            mod.score_batch(data, stats)
        counts = allocspy.stop()
        obj_malloc, _, obj_realloc, obj_free = counts["obj"]
        print(
            f"{variant:<8}{obj_malloc / records:>19.2f}{obj_realloc / records:>20.2f}{obj_free / records:>17.2f}"
            f"{sum(counts['mem'][:3]) / records:>18.2f}{sum(counts['raw'][:3]) / records:>18.2f}"
        )

    # tracemalloc keeps one trace per live block, so memory that is allocated
    # and freed inside the call leaves nothing behind in a snapshot.
    mod = importlib.import_module("recscore_regr")
    stats = {}
    mod.score_batch(data, stats)
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for _ in range(10):
        mod.score_batch(data, stats)
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    gained = sum(max(d.count_diff, 0) for d in after.compare_to(before, "lineno"))
    churn = 10 * RECORDS_PER_BATCH
    print(
        f"tracemalloc snapshot diff over {churn} regr records: at least {churn} new live blocks: {yn(gained >= churn)}"
    )
    print(
        f"tracemalloc snapshot diff over {churn} regr records: fewer than 1000 new live blocks: {yn(gained < 1000)}"
    )
    print()


def phase_heartbeat(mods: dict, data: bytes) -> None:
    """A Python thread that only needs the GIL for a moment every millisecond."""
    print("== Phase 5: heartbeat thread during one long call ==")
    big = data * 400  # 800000 records in a single score_batch call
    for variant in ("hold", "base", "regr"):
        mod = mods[variant]
        stop = threading.Event()
        gaps: list[float] = []
        t = threading.Thread(target=heartbeat, args=(stop, gaps))
        t.start()
        time.sleep(0.05)
        gaps.clear()
        t0 = time.perf_counter()
        mod.score_batch(big, {})
        elapsed = time.perf_counter() - t0
        time.sleep(0.01)
        stop.set()
        t.join()
        stalled = max(gaps) >= 0.5 * elapsed
        print(
            f"{variant}: heartbeat stalled for at least half of the call: {yn(stalled)}"
        )
    print()


def sample_service(build_dir: str, variant: str) -> dict[str, int]:
    """Run service.py and take a 2 second sample(1) of its native stacks."""
    proc = subprocess.Popen(
        [
            sys.executable,
            str(HERE / "service.py"),
            "--build-dir",
            build_dir,
            "--variant",
            variant,
            "--threads",
            str(THREADS),
            "--seconds",
            "60",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, "sample.txt")
            subprocess.run(
                ["sample", str(proc.pid), "2", "-file", report],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=120,
            )
            with open(report, encoding="utf-8", errors="replace") as fh:
                return summarize(fh.read())
    finally:
        proc.kill()
        proc.wait()


def phase_native_stacks(build_dir: str) -> None:
    print(f"== Phase 6: native stacks of the running service, {THREADS} threads ==")
    if platform.system() != "Darwin" or shutil.which("sample") is None:
        print(
            "skipped: sample(1) is a macOS tool; on Linux use perf or the Tachyon commands in the note"
        )
        print()
        return
    regr = sample_service(build_dir, "regr")
    fixed = sample_service(build_dir, "fixed")
    print(
        f"regr: worker threads sampled inside score_batch: {yn(regr['worker_samples'] > 0)}"
    )
    print(
        "regr: at least 50% of worker samples are under score_batch -> PyGILState_Ensure: "
        f"{yn(regr['under_ensure'] >= 0.5 * regr['worker_samples'])}"
    )
    print(
        "regr: at least 90% of those are inside take_gil: "
        f"{yn(regr['under_ensure'] > 0 and regr['under_take_gil'] >= 0.9 * regr['under_ensure'])}"
    )
    print(
        f"fixed: worker threads sampled inside score_batch: {yn(fixed['worker_samples'] > 0)}"
    )
    print(
        f"fixed: worker samples under score_batch -> PyGILState_Ensure: {fixed['under_ensure']}"
    )
    print()


def phase_fix(mods: dict, data: bytes, runs: dict) -> None:
    print("== Phase 7: the fix keeps the output and restores scaling ==")
    checksums = {v: mods[v].score_batch(data, {}) for v in VARIANTS}
    print(
        f"checksum identical across all five variants: {yn(len(set(checksums.values())) == 1)}"
    )
    per_variant = {}
    for variant in ("regr", "gilfix", "fixed"):
        stats: dict = {}
        for _ in range(3):
            mods[variant].score_batch(data, stats)
        per_variant[variant] = stats
    print(
        f"per-venue stats identical in regr, gilfix, and fixed: "
        f"{yn(per_variant['regr'] == per_variant['gilfix'] == per_variant['fixed'])}"
    )
    print(
        f"per-venue stats count every record: {yn(sum(per_variant['fixed'].values()) == 3 * RECORDS_PER_BATCH)}"
    )
    baseN = runs["base", THREADS].records_per_s
    for variant in ("gilfix", "fixed"):
        one, many = runs[variant, 1].records_per_s, runs[variant, THREADS].records_per_s
        print(
            f"{variant}: {THREADS} threads at least 2.0x the throughput of 1 thread: {yn(many >= 2.0 * one)}"
        )
        print(
            f"{variant}: {THREADS}-thread throughput at least half of base: {yn(many >= 0.5 * baseN)}"
        )


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    build_dir = sys.argv[1]
    sys.path.insert(0, build_dir)
    gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)()
    if not gil_enabled:
        print(
            "drill.py: this interpreter runs without a GIL (free-threaded build); "
            "the drill needs a build with the GIL",
            file=sys.stderr,
        )
        return 1
    print(f"interpreter has a GIL: {yn(gil_enabled)}")
    print(f"switch interval: {sys.getswitchinterval() * 1000:g} ms")
    print()
    data = make_payload()
    mods = {v: importlib.import_module(f"recscore_{v}") for v in VARIANTS}
    runs = measure(mods, data)
    phase_symptom(runs)
    phase_probe(build_dir, data)
    phase_allocators(data)
    phase_heartbeat(mods, data)
    phase_native_stacks(build_dir)
    phase_fix(mods, data, runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
