#!/usr/bin/env python3
"""Triage three injected tail-latency faults from outside the process, with no eBPF and no perf.

The playbook's privileged tools (runqlat, offcputime, perf record) need CAP_BPF and CAP_PERFMON,
which an unprivileged container does not have. This driver walks the same decision tree with the
kernel's own per-thread accounting, which any user may read for their own processes:

  /proc/PID/task/TID/schedstat   on-CPU ns, run-queue wait ns, timeslices (what runqlat histograms)
  /proc/PID/task/TID/syscall     the syscall a blocked thread is in ("running" when it is not blocked)
  /proc/PID/task/TID/stat        field 39, the CPU the thread last ran on
  eu-stack -1 -p TID             one user stack per call: a wall-clock sampler built from ptrace

Stdout carries derived facts only (yes/no, grew/flat, names), so that it is the same on every run.
The raw numbers of the run go to stderr.

Every measurement passes an environment gate before it is used. The simulator's requests cost a known
amount of CPU time, so a window whose accounted CPU per request is far from that, or in which the
worker could not keep up with its schedule, was taken while the host starved the machine (a VM whose
host steals time accounts the stolen time as on-CPU). The worker's run-queue delay must also be
explained by CPU time that threads visible in this PID namespace spent on its CPU; delay beyond that
was caused by a neighbor the lab cannot see. Such a window is measured again, and after ATTEMPTS
failures the lab exits non-zero instead of printing facts about the host's noise.
"""

from __future__ import annotations

import collections
import glob
import os
import random
import subprocess
import sys
import time

SLO_US = 20_000          # must match SLO_NS in spike.c
SLOW_PATH_US_PER_REQUEST = 60_000 / 250  # SLOW_PATH_CPU_NS / SLOW_PATH_EVERY in spike.c, in us
BASELINE_CPU_US = (90.0, 190.0)  # plausible CPU per request with no fault: 100 us plus overhead
WINDOW_S = 6.0           # one measurement window
SETTLE_S = 0.5           # let a fault reach steady state before measuring
ATTEMPTS = 3             # a window that fails the environment gate is measured again
UNEXPLAINED_RUNQ_POINTS = 5.0  # run-queue delay beyond what visible threads' CPU time explains
STACK_SAMPLES = 200      # eu-stack samples per profiled phase
GREW_POINTS = 10.0       # a component "grew" when its share of wall time rose by this many points
PHASES = ("baseline", "runq", "lock", "oncpu", "recovered")
FAULT_OF = {"baseline": "none", "recovered": "none"}
VERDICTS = {
    (): "no regression in the worker's time split",
    ("run-queue",): "run-queue delay: the CPU is saturated or the group is throttled",
    ("futex-wait",): "blocked on a lock: off-CPU in a futex wait",
    ("on-CPU",): "more on-CPU work per request",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Service:
    """The spike process and the /proc paths of its threads."""

    def __init__(self, binary: str) -> None:
        self.proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        ready = dict(kv.split("=") for kv in self._readline().split()[1:])
        self.pid = int(ready["pid"])
        self.sys_futex = ready["sys_futex"]
        self.sys_sleep = ready["sys_clock_nanosleep"]
        self.threads: dict[str, list[str]] = collections.defaultdict(list)
        for task in sorted(glob.glob(f"/proc/{self.pid}/task/*")):
            with open(f"{task}/comm") as f:
                self.threads[f.read().strip()].append(task)
        if len(self.threads["worker"]) != 1:
            raise SystemExit("expected exactly one thread named worker")
        self.worker = self.threads["worker"][0]
        self.worker_tid = self.worker.rsplit("/", 1)[1]

    def _readline(self) -> str:
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit("spike exited unexpectedly")
        return line.strip()

    def command(self, text: str) -> str:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        reply = self._readline()
        if reply.startswith("error"):
            raise SystemExit(f"spike rejected {text!r}: {reply}")
        return reply

    def stop(self) -> None:
        self.proc.stdin.write("quit\n")
        self.proc.stdin.flush()
        self.proc.wait(timeout=30)


def schedstat(task: str) -> tuple[int, int, int]:
    with open(f"{task}/schedstat") as f:
        cpu_ns, runq_ns, slices = f.read().split()
    return int(cpu_ns), int(runq_ns), int(slices)


def last_cpu(task: str) -> int:
    with open(f"{task}/stat") as f:
        after_comm = f.read().rsplit(")", 1)[1].split()
    return int(after_comm[36])  # field 39 of proc_pid_stat(5); the split starts at field 3


def all_thread_cpu_ns() -> dict[str, tuple[str, int]]:
    """On-CPU time of every thread visible in /proc, keyed by task directory."""
    out = {}
    for task in glob.glob("/proc/[0-9]*/task/[0-9]*"):
        try:
            with open(f"{task}/comm") as f:
                comm = f.read().strip()
            out[task] = (comm, schedstat(task)[0])
        except OSError:
            continue  # the thread exited between the listing and the read
    return out


def wait_site(task: str, svc: Service) -> str:
    """Classify what the thread is doing right now from the first field of its syscall file."""
    with open(f"{task}/syscall") as f:
        first = f.read().split()[0]
    if first == "running":
        return "runnable"       # on a CPU or waiting for one: schedstat tells the two apart
    if first == svc.sys_futex:
        return "futex"
    if first == svc.sys_sleep:
        return "timer-sleep"    # the worker's idle wait for the next request
    return "other"


def observe(svc: Service, body) -> dict:
    """Run body() inside one measurement window of the service and account the worker's time over it."""
    svc.command("reset")
    before_all = all_thread_cpu_ns()
    cpu0, runq0, _ = schedstat(svc.worker)
    t0 = time.monotonic()
    result = body()
    wall_ns = (time.monotonic() - t0) * 1e9
    cpu1, runq1, _ = schedstat(svc.worker)
    after_all = all_thread_cpu_ns()
    report = dict(kv.split("=") for kv in svc.command("report").split()[1:])

    cpu_of_worker = last_cpu(svc.worker)
    rivals: collections.Counter[str] = collections.Counter()
    rival_threads: collections.Counter[str] = collections.Counter()
    for task, (comm, ns) in after_all.items():
        delta = ns - before_all.get(task, (comm, ns))[1]
        if task == svc.worker or delta <= 0:
            continue
        try:
            same_cpu = last_cpu(task) == cpu_of_worker
        except OSError:
            continue
        if same_cpu:
            rivals[comm] += delta
            rival_threads[comm] += 1
    n = int(report["n"])
    return {
        "result": result,
        "n": n,
        "expected_n": wall_ns / 1e6,  # one request per millisecond
        "p50_us": int(report["p50_us"]),
        "p99_us": int(report["p99_us"]),
        "slow_intended": int(report["slow_intended"]),
        "slow_service": int(report["slow_service"]),
        "on-CPU": 100.0 * (cpu1 - cpu0) / wall_ns,
        "run-queue": 100.0 * (runq1 - runq0) / wall_ns,
        "cpu_ns": cpu1 - cpu0,
        "unexplained_runq": 100.0 * max(0, (runq1 - runq0) - sum(rivals.values())) / wall_ns,
        "cpu_us_per_request": (cpu1 - cpu0) / 1000.0 / max(n, 1),
        "rivals": rivals,
        "rival_threads": rival_threads,
    }


def sample_wait_sites(svc: Service) -> collections.Counter[str]:
    rng = random.Random(7)
    sites: collections.Counter[str] = collections.Counter()
    t0 = time.monotonic()
    while time.monotonic() - t0 < WINDOW_S:
        sites[wait_site(svc.worker, svc)] += 1
        time.sleep(rng.uniform(0.0015, 0.0035))  # jittered so it cannot lock onto the 1 ms request period
    return sites


def gate(phase: str, what: str, r: dict, base: dict | None) -> str | None:
    """Why this window cannot be trusted, or None. See the module docstring."""
    if r["n"] < 0.9 * r["expected_n"]:
        return f"the worker served {r['n']} of {r['expected_n']:.0f} scheduled requests"
    if base is None:
        low, high = BASELINE_CPU_US
    else:
        nominal = base["cpu_us_per_request"] + (SLOW_PATH_US_PER_REQUEST if phase == "oncpu" else 0.0)
        low, high = 0.6 * nominal, 1.5 * nominal
    if not low <= r["cpu_us_per_request"] <= high:
        return (f"{r['cpu_us_per_request']:.0f} us of CPU per request is outside {low:.0f} to {high:.0f}: "
                "the host is stealing CPU time from this machine")
    if r["unexplained_runq"] > UNEXPLAINED_RUNQ_POINTS:
        return (f"{r['unexplained_runq']:.1f} points of run-queue delay that no thread visible here accounts for: "
                "something outside this PID namespace is using the worker's CPU")
    # eu-stack stops the thread, so only the latency window is held to the SLO.
    if what == "window" and phase in FAULT_OF and r["p99_us"] > SLO_US:
        return f"p99 is {r['p99_us']} us with no fault injected"
    return None


def gated(svc: Service, phase: str, what: str, base: dict | None, body) -> dict:
    for attempt in range(1, ATTEMPTS + 1):
        r = observe(svc, body)
        why = gate(phase, what, r, base)
        log(f"{phase:9s} {what} attempt {attempt}: n={r['n']} p50={r['p50_us']}us p99={r['p99_us']}us "
            f"slow intended/service={r['slow_intended']}/{r['slow_service']} on-CPU={r['on-CPU']:.1f}% "
            f"run-queue={r['run-queue']:.1f}% (unexplained {r['unexplained_runq']:.1f}) "
            f"cpu/request={r['cpu_us_per_request']:.0f}us"
            + (f" REJECTED: {why}" if why else ""))
        if why is None:
            return r
    raise SystemExit(f"environment gate: {phase} {what} failed {ATTEMPTS} times, last reason: {why}. "
                     "Either the host is too loaded for a timing lab or the fault did not behave; "
                     "no facts were printed.")


def sample_stacks(svc: Service, app_symbols: set[str]) -> collections.Counter[str]:
    """Wall-clock stack samples of the worker thread, folded root-first like stackcollapse output."""
    rng = random.Random(11)
    folded: collections.Counter[str] = collections.Counter()
    for _ in range(STACK_SAMPLES):
        out = subprocess.run(["eu-stack", "-1", "-p", svc.worker_tid], capture_output=True, text=True, check=False)
        frames = []
        for line in out.stdout.splitlines():
            if line.startswith("#"):
                parts = line.split()
                frames.append(parts[2] if len(parts) > 2 else "[unknown]")
        if not frames:
            raise SystemExit(f"eu-stack printed no frames (exit {out.returncode}): {out.stderr.strip()}")
        frames.reverse()  # eu-stack prints the leaf first
        # Keep the application's frames and the one library frame it called: enough to name a wait.
        last_app = max((i for i, name in enumerate(frames) if name in app_symbols), default=-1)
        first_app = min((i for i, name in enumerate(frames) if name in app_symbols), default=0)
        folded[";".join(frames[first_app:last_app + 2])] += 1
        time.sleep(rng.uniform(0.003, 0.017))
    return folded


def difffolded(before: collections.Counter[str], after: collections.Counter[str]) -> dict[str, tuple[int, int]]:
    """The algorithm of FlameGraph's difffolded.pl -n: scale the first profile to the second's total."""
    total1, total2 = sum(before.values()), sum(after.values())
    out = {}
    for stack in set(before) | set(after):
        c1, c2 = before.get(stack, 0), after.get(stack, 0)
        if total1 != total2:
            c1 = int(c1 * total2 / total1)
        out[stack] = (c1, c2)
    return out


def biggest_growth(diff: dict[str, tuple[int, int]]) -> tuple[str, float]:
    total = sum(c2 for _, c2 in diff.values())
    stack, (c1, c2) = max(diff.items(), key=lambda kv: kv[1][1] - kv[1][0])
    return stack, 100.0 * (c2 - c1) / total


def app_symbols_of(binary: str) -> set[str]:
    out = subprocess.run(["nm", "--defined-only", binary], capture_output=True, text=True, check=True)
    return {parts[2] for parts in (line.split() for line in out.stdout.splitlines())
            if len(parts) == 3 and parts[1] in "Tt"}


def yes(flag: bool) -> str:
    return "yes" if flag else "no"


def main() -> int:
    binary, workdir = sys.argv[1], sys.argv[2]
    app_symbols = app_symbols_of(binary)
    svc = Service(binary)
    results: dict[str, dict] = {}
    stacks: dict[str, collections.Counter[str]] = {}
    try:
        for phase in PHASES:
            svc.command(f"fault {FAULT_OF.get(phase, phase)}")
            time.sleep(SETTLE_S)
            base = results.get("baseline")
            r = gated(svc, phase, "window", base, lambda: sample_wait_sites(svc))
            sites = r["result"]
            n_sites = sum(sites.values())
            r["futex-wait"] = 100.0 * sites["futex"] / n_sites
            r["timer-sleep"] = 100.0 * sites["timer-sleep"] / n_sites
            log(f"{phase:9s} wait sites: " + ", ".join(f"{k}={100.0 * v / n_sites:.1f}%" for k, v in sites.most_common()))
            results[phase] = r
            if phase in ("baseline", "lock", "oncpu"):
                # The stack samples are taken in a window of their own: each eu-stack call stops the
                # thread, which would distort the latency numbers of the window above.
                profiled = gated(svc, phase, "stacks", results["baseline"] if phase != "baseline" else None,
                                 lambda: sample_stacks(svc, app_symbols))
                stacks[phase] = profiled["result"]
                with open(os.path.join(workdir, f"{phase}.folded"), "w") as f:
                    for stack, count in sorted(stacks[phase].items()):
                        f.write(f"{stack} {count}\n")
                log(f"{phase:9s} stacks: " + ", ".join(f"{s}={c}" for s, c in stacks[phase].most_common()))
    finally:
        svc.stop()

    base = results["baseline"]
    if base["cpu_ns"] <= 0:
        raise SystemExit("schedstat shows no on-CPU time for a busy thread: this kernel lacks CONFIG_SCHED_INFO")

    print("== Stage 1: confirm the tail (open loop, 1000 requests/s, SLO 20 ms from the intended start) ==")
    print(f"{'phase':<10} {'p50 under SLO':<14} p99 under SLO")
    for phase in PHASES:
        r = results[phase]
        print(f"{phase:<10} {yes(r['p50_us'] <= SLO_US):<14} {yes(r['p99_us'] <= SLO_US)}")

    print()
    print("== Stage 2: coordinated omission (lock phase) ==")
    lock = results["lock"]
    print("requests over SLO, timed from the intended start, outnumber those timed from the actual start 5 to 1: "
          + yes(lock["slow_intended"] >= 5 * max(lock["slow_service"], 1)))
    print("timing from the actual start still sees some slow requests: " + yes(lock["slow_service"] > 0))

    print()
    print("== Stage 3: where the worker's wall time went (growth against baseline, in points of wall time) ==")
    print(f"{'phase':<10} {'on-CPU':<7} {'run-queue':<10} {'futex-wait':<11} verdict")
    components = ("on-CPU", "run-queue", "futex-wait")
    grew_in: dict[str, tuple[str, ...]] = {}
    for phase in PHASES[1:]:
        r = results[phase]
        grew = tuple(c for c in components if r[c] - base[c] >= GREW_POINTS)
        grew_in[phase] = grew
        cells = ["GREW" if c in grew else "flat" for c in components]
        verdict = VERDICTS.get(tuple(sorted(grew)), "mixed: " + ", ".join(grew))
        print(f"{phase:<10} {cells[0]:<7} {cells[1]:<10} {cells[2]:<11} {verdict}")
    print("total off-CPU share (timer sleep plus futex wait) moved by less than 10 points in the lock phase: "
          + yes(abs((lock["timer-sleep"] + lock["futex-wait"]) - (base["timer-sleep"] + base["futex-wait"]))
                < GREW_POINTS))

    print()
    print("== Stage 4: name the culprit ==")
    runq = results["runq"]
    if runq["rivals"]:
        comm, ns = runq["rivals"].most_common(1)[0]
        print(f"runq:  busiest other thread name on the worker's CPU: {comm} "
              f"({runq['rival_threads'][comm]} threads, more CPU time than the worker: {yes(ns > runq['cpu_ns'])})")
    else:
        print("runq:  no other thread ran on the worker's CPU")
    for phase in ("lock", "oncpu"):
        diff = difffolded(stacks["baseline"], stacks[phase])
        with open(os.path.join(workdir, f"{phase}.diff"), "w") as f:
            for stack, (c1, c2) in sorted(diff.items()):
                f.write(f"{stack} {c1} {c2}\n")
        stack, points = biggest_growth(diff)
        log(f"{phase:9s} biggest growth: {stack} (+{points:.1f} points of samples)")
        if points < GREW_POINTS:
            print(f"{phase}: no stack grew by {GREW_POINTS:.0f} points of samples")
            continue
        frames = stack.split(";")
        leaf = frames[-1]
        where = "application code" if leaf in app_symbols else "library call made by " + frames[-2]
        print(f"{phase + ':':<6} stack with the largest growth: {stack}")
        print(f"{'':<6} leaf frame {leaf} is {where}")

    print()
    print("== Stage 5: verify the mitigation (fault removed) ==")
    rec = results["recovered"]
    print("p99 back under SLO: " + yes(rec["p99_us"] <= SLO_US))
    print("time split back to baseline: " + yes(not grew_in["recovered"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
