"""Tracing versus sampling in CPython: what each profiler sees and what it costs.

Every line printed is a derived fact (a count that cannot vary, a ratio bucket, or a
pass/fail property), so the output is stable across runs. Each check can fail: if the
mechanism it names were absent, the printed line would differ.

Sections:
  1. How profiling.tracing (cProfile) hooks the interpreter.
  2. Exact call counts, including the recursive "total/primitive" form.
  3. Observer effect: cProfile inflates call-heavy code; a sampler does not.
  4. Tachyon's own collectors: times are sample counts times the interval.
  5. In-process sampling sees the GIL through its own hands.
  6. Async-aware stack reconstruction (task graph instead of thread stack).
  7. Out-of-process attach: what the OS demands.
  8. How many samples a percentage needs.
"""

import asyncio
import cProfile
import math
import os
import pstats
import re
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import warnings

HAVE_315 = sys.version_info >= (3, 15)
if HAVE_315:
    import _remote_debugging as rd
    from profiling.sampling import CollapsedStackCollector, PstatsCollector
    from profiling.sampling.collector import iter_async_frames

FREE_THREADED = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
SAMPLE_INTERVAL_S = 0.001  # the sampler asks for 1 kHz, Tachyon's default rate


def header(title):
    print()
    print(f"[{title}]")


def needs_315():
    print("  needs Python 3.15 (profiling package and _remote_debugging)")


# ---------------------------------------------------------------- workloads


def add(a, b):
    return a + b


def call_heavy(n):
    s = 0
    for i in range(n):
        s = add(s, i)
    return s


def loop_heavy(n):
    s = 0
    for i in range(n):
        s = s + i
    return s


def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)


def two_line_work(n):
    s = 0
    for i in range(n):
        s += i * i
    for i in range(n):
        s -= i
    return s


def median_time(fn, n, repeat=5):
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn(n)
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def stats_by_name(prof):
    """Map function name to the pstats tuple (cc, nc, tt, ct, callers)."""
    return {key[2]: value for key, value in pstats.Stats(prof).stats.items()
            if key[0] == __file__}  # stdlib has its own add(), e.g. _weakrefset


# ---------------------------------------------------------------- sampler


class SelfSampler:
    """Samples this process's main thread with Tachyon's RemoteUnwinder.

    task_for_pid() on our own PID needs no privilege on macOS, and process_vm_readv()
    on our own PID needs none on Linux, so this runs without sudo. The price is that
    the sampler thread is in-process and must hold the GIL to run.
    """

    def __init__(self):
        self.unwinder = rd.RemoteUnwinder(os.getpid())
        self.samples = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sampler")

    def _loop(self):
        # A fixed sleep, not a catch-up schedule: a late sampler that fires back-to-back
        # while it still holds the GIL records the same stack several times.
        while not self._stop.is_set():
            self.samples.append(self.unwinder.get_stack_trace())
            time.sleep(SAMPLE_INTERVAL_S)

    def __enter__(self):
        self.t0 = time.perf_counter()
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self.elapsed = time.perf_counter() - self.t0
        return False

    def main_threads(self):
        for sample in self.samples:
            for interp in sample:
                for th in interp.threads:
                    yield th


# ---------------------------------------------------------------- sections


def section_mechanism():
    header("1 tracing hook")
    print(f"  interpreter: CPython {sys.version_info[0]}.{sys.version_info[1]}"
          f" ({'free-threaded' if FREE_THREADED else 'GIL'} build)")
    if HAVE_315:
        import profiling.tracing
        print(f"  profiling.tracing.Profile is cProfile.Profile: {profiling.tracing.Profile is cProfile.Profile}")
    else:
        needs_315()
    mon = getattr(sys, "monitoring", None)
    prof = cProfile.Profile()
    prof.enable()
    try:
        tool = mon.get_tool(mon.PROFILER_ID) if mon else None
        hook = sys.getprofile()
        try:
            second = cProfile.Profile()
            second.enable()
            second.disable()
            clash = "second enable() succeeded"
        except ValueError as err:
            clash = f"ValueError: {err}"
    finally:
        prof.disable()
    if mon:
        print(f"  sys.monitoring tool {mon.PROFILER_ID} while enabled: {tool!r}")
    print(f"  sys.setprofile hook while enabled: {type(hook).__name__}")
    print(f"  second Profile().enable() while one is active: {clash}")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sys.modules.pop("profile", None)
        import profile  # noqa: F401
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    print(f"  import profile raises DeprecationWarning: {bool(deprecations)}")


def section_exact_counts():
    header("2 exact counts")
    prof = cProfile.Profile()
    prof.enable()
    fib(15)
    call_heavy(1000)
    prof.disable()
    st = stats_by_name(prof)
    cc, nc = st["fib"][0], st["fib"][1]
    print(f"  fib(15): ncalls {nc}/{cc} (total/primitive); 2*F(16)-1 = {2 * 987 - 1}")
    print(f"  add called {st['add'][1]} times, all from call_heavy: "
          f"{list(k[2] for k in st['add'][4])}")
    print("  re-running gives identical counts: "
          f"{stats_by_name(run_profiled(fib, 15))['fib'][1] == nc}")

    async def ten_awaits():
        for _ in range(10):
            await asyncio.sleep(0)

    st = stats_by_name(run_profiled(asyncio.run, ten_awaits()))
    print(f"  coroutine with 10 awaits: ncalls {st['ten_awaits'][1]} (first start + one per PY_RESUME)")

    def worker():
        for _ in range(100):
            add(1, 2)

    def in_thread(_):
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    st = stats_by_name(run_profiled(in_thread, None))
    print(f"  calls made by another thread recorded: {st.get('worker', (0, 0))[1]} worker,"
          f" {st.get('add', (0, 0))[1]} add")


def run_profiled(fn, arg):
    prof = cProfile.Profile()
    prof.enable()
    fn(arg)
    prof.disable()
    return prof


def bucket(value, edges):
    """Return a label 'a <= x < b' for the bucket containing value."""
    lo = None
    for edge in edges:
        if value < edge:
            return f"< {edge}" if lo is None else f"{lo} to {edge}"
        lo = edge
    return f">= {lo}"


def section_observer_effect():
    header("3 observer effect")
    n = 300_000
    t_call = median_time(call_heavy, n)
    m = int(n * t_call / median_time(loop_heavy, n))  # calibrate loop_heavy to equal cost
    t_loop = median_time(loop_heavy, m)
    print("  calibrated: call_heavy and loop_heavy take equal time unprofiled "
          f"(ratio {bucket(t_call / t_loop, [0.67, 1.5])})")

    prof = cProfile.Profile()
    t0 = time.perf_counter()
    prof.enable()
    call_heavy(n)
    prof.disable()
    p_call = time.perf_counter() - t0
    t0 = time.perf_counter()
    prof.enable()
    loop_heavy(m)
    prof.disable()
    p_loop = time.perf_counter() - t0
    st = stats_by_name(prof)
    share = st["call_heavy"][3] / (st["call_heavy"][3] + st["loop_heavy"][3])
    print(f"  cProfile slowdown, call_heavy ({n} calls to add): {bucket(p_call / t_call, [1.5, 2.5, 6.0])}x")
    print(f"  cProfile slowdown, loop_heavy (no calls):        {bucket(p_loop / t_loop, [1.5, 2.5, 6.0])}x")
    print(f"  cProfile cumtime share of call_heavy (truth 50%): {bucket(share * 100, [35, 65, 90])}%")

    if not HAVE_315:
        needs_315()
        return None
    rounds = 100
    with SelfSampler() as sampler:
        for _ in range(rounds):
            call_heavy(n)
            loop_heavy(m)
    counts = {"call_heavy": 0, "loop_heavy": 0}
    for th in sampler.main_threads():
        names = {f.funcname for f in th.frame_info}
        for name in counts:
            if name in names:
                counts[name] += 1
    hits = counts["call_heavy"] + counts["loop_heavy"]
    s_share = counts["call_heavy"] / hits if hits else 0.0
    rate = len(sampler.samples) / sampler.elapsed
    print(f"  sampler share of call_heavy (truth 50%):          {bucket(s_share * 100, [35, 65, 90])}%")
    print(f"  sampler slowdown of the whole run:                {bucket(sampler.elapsed / (rounds * (t_call + t_loop)), [1.5, 2.5, 6.0])}x")
    print(f"  samples in window: {bucket(hits, [100, 200])}; requested 1000 Hz, achieved {bucket(rate, [400, 900])} Hz")
    return sampler


def section_collectors(sampler):
    header("4 Tachyon collectors")
    if not HAVE_315:
        needs_315()
        return
    with SelfSampler() as s2:
        two_line_work(4_000_000)
    interval_us = int(SAMPLE_INTERVAL_S * 1_000_000)
    pc = PstatsCollector(interval_us)
    for sample in sampler.samples + s2.samples:
        pc.collect(sample)
    pc.create_stats()
    exact = all(
        math.isclose(tt, cc * SAMPLE_INTERVAL_S) and math.isclose(ct, nc * SAMPLE_INTERVAL_S)
        for (cc, nc, tt, ct, _callers) in pc.stats.values()
    )
    print(f"  every row: tottime == direct samples * interval and cumtime == cumulative samples * interval: {exact}")
    rows = sorted(key[1] - two_line_work.__code__.co_firstlineno
                  for key in pc.stats if key[2] == "two_line_work")
    print(f"  Tachyon pstats rows for two_line_work: {len(rows)}, keyed by sampled line (def + {rows})")
    cprof_rows = [k for k in pstats.Stats(run_profiled(two_line_work, 10)).stats if k[2] == "two_line_work"]
    print(f"  cProfile rows for two_line_work: {len(cprof_rows)}, keyed by the def line (def + {cprof_rows[0][1] - two_line_work.__code__.co_firstlineno})")

    cs = CollapsedStackCollector(interval_us)
    for sample in sampler.samples:
        cs.collect(sample)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "stacks.txt")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            cs.export(path)
        with open(path) as f:
            lines = [line.rstrip("\n") for line in f if line.strip()]
    pattern = re.compile(r"^tid:\d+(;[^;]+:[^;]+:\d+)+ \d+$")
    print(f"  collapsed lines all match 'tid:N;file:func:line;... count': {all(pattern.match(x) for x in lines)}")
    deepest = [x for x in lines if ":add:" in x]
    if deepest:
        shown = re.sub(r"^tid:\d+", "tid:<TID>", re.sub(r" \d+$", " <COUNT>", deepest[0]))
        print(f"  stack through add: {shown}")


def section_gil(sampler):
    header("5 in-process GIL view")
    if not HAVE_315:
        needs_315()
        return
    if FREE_THREADED:
        print("  free-threaded build: no GIL, section skipped")
        return
    total = has_gil = requested = 0
    for th in sampler.main_threads():
        total += 1
        has_gil += bool(th.status & rd.THREAD_STATUS_HAS_GIL)
        requested += bool(th.status & rd.THREAD_STATUS_GIL_REQUESTED)
    print(f"  main thread had THREAD_STATUS_HAS_GIL in {has_gil} samples (the sampler held it every time)")
    print(f"  main thread had THREAD_STATUS_GIL_REQUESTED in some samples: {requested > 0}")
    print(f"  sampler holds GIL during its own read: {own_status_has_gil()}")


def own_status_has_gil():
    u = rd.RemoteUnwinder(os.getpid(), all_threads=True)
    me = threading.get_native_id()
    for interp in u.get_stack_trace():
        for th in interp.threads:
            if th.thread_id == me:
                return bool(th.status & rd.THREAD_STATUS_HAS_GIL)
    return None


async def _leaf(delay):
    await asyncio.sleep(delay)


async def fetch_user():
    await _leaf(1.0)


async def fetch_orders():
    await _leaf(1.0)


async def handler():
    await asyncio.gather(fetch_user(), fetch_orders())


def section_async():
    header("6 async-aware stacks")
    if not HAVE_315:
        needs_315()
        return
    ready = threading.Event()
    out = {}

    def snapshot():
        ready.wait()
        u = rd.RemoteUnwinder(os.getpid())
        out["plain"] = u.get_stack_trace()
        try:
            out["tasks"] = u.get_all_awaited_by()
        except RuntimeError as err:
            out["error"] = f"{type(err).__name__}: {err}"

    async def main():
        t1 = asyncio.create_task(handler(), name="request-1")
        t2 = asyncio.create_task(fetch_orders(), name="batch-2")
        for _ in range(5):
            await asyncio.sleep(0)  # let every task reach its sleep
        ready.set()
        await asyncio.gather(t1, t2)

    th = threading.Thread(target=snapshot)
    th.start()
    asyncio.run(main())
    th.join()
    plain = [f.funcname for f in out["plain"][0].threads[0].frame_info]
    user = {"fetch_user", "fetch_orders", "handler", "_leaf"}
    print(f"  plain thread stack top frame: {plain[0]}; user coroutine frames in it: {len(user & set(plain))}")
    if "error" in out:
        print(f"  task graph: unavailable on this build ({out['error']})")
        return
    chains = sorted(" <- ".join(f.funcname for f in frames)
                    for frames, _tid, _leaf_id in iter_async_frames(out["tasks"]))
    for chain in chains:
        print(f"  task chain: {re.sub(r'Task-[0-9]+', 'Task-N', chain)}")


def section_attach(target):
    header("7 out-of-process attach")
    if not HAVE_315:
        needs_315()
        return
    proc = subprocess.run(
        [sys.executable, "-m", "profiling.sampling", "run", "-d", "2", "--no-summary", "-l", "3", target],
        capture_output=True, text=True, timeout=120,
    )
    text = proc.stdout + proc.stderr
    if "Permission" in text:
        print(f"  profiling.sampling run as uid {'0' if os.geteuid() == 0 else 'non-root'}: "
              f"denied, exit status {proc.returncode}")
        print("  reason: reading another task's memory (task_for_pid / ptrace access mode) needs privilege")
    elif proc.returncode == 0 and "Captured" in text:
        print("  profiling.sampling run: sampled the child out of process, exit status 0")
        print(f"  busy() is in the top rows: {'busy' in proc.stdout}")
    else:
        print(f"  profiling.sampling run: unexpected result, exit status {proc.returncode}")


def section_statistics():
    header("8 samples needed")
    for p, n in ((0.05, 1_000), (0.05, 100_000), (0.50, 1_000)):
        se = math.sqrt(p * (1 - p) / n)
        print(f"  p={p:.2f} n={n:>7}: 1.96*SE = +/-{196 * se:.2f} points, 3*SE = +/-{300 * se:.2f} points")


def main():
    target = sys.argv[1]
    section_mechanism()
    section_exact_counts()
    sampler = section_observer_effect()
    section_collectors(sampler)
    section_gil(sampler)
    section_async()
    section_attach(target)
    section_statistics()


if __name__ == "__main__":
    main()
