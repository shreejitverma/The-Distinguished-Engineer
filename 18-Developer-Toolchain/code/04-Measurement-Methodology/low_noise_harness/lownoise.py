"""A small low-noise A/B benchmark harness.

Four parts, each usable on its own:

- preflight():       read the host controls that matter for one benchmark CPU and report findings.
- interleaved_order: a seeded, randomized, pairwise-interleaved A/B schedule.
- run_sample():      run one fresh process pinned to one CPU, with ASLR off when the kernel allows it.
- compare():         Mann-Whitney U test plus a Hodges-Lehmann shift estimate, and a verdict.

Only random.Random.random() is used for randomness: the Python documentation guarantees that
its sequence for a given seed does not change between versions, which is not true of shuffle().
"""

from __future__ import annotations

import ctypes
import functools
import math
import os
import random
import statistics
import subprocess
from pathlib import Path
from typing import NamedTuple

ADDR_NO_RANDOMIZE = 0x0040000  # include/uapi/linux/personality.h
CPU_DIR = "sys/devices/system/cpu"


# --------------------------------------------------------------------------- host preflight


def parse_cpulist(text: str) -> list[int]:
    """Parse the kernel's cpulist output format ("0-3,8,10-11"); an empty string is no CPUs."""
    cpus: set[int] = set()
    for part in text.replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        low, dash, high = part.partition("-")
        first, last = int(low), int(high) if dash else int(low)
        if last < first:
            raise ValueError(f"descending range in cpulist: {part!r}")
        cpus.update(range(first, last + 1))
    return sorted(cpus)


def format_cpulist(cpus: list[int]) -> str:
    return ",".join(str(c) for c in cpus) if cpus else "none"


class Finding(NamedTuple):
    check: str
    status: str  # "ok", "fail", "unknown" (the control is not exposed), or "note" (optional control)
    detail: str


def _read(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_text().strip()
    except OSError:
        return None


def preflight(root: Path, cpu: int) -> list[Finding]:
    """Check the controls for one benchmark CPU. `root` is "/" on a real host, a fixture tree in tests."""
    out: list[Finding] = []

    governor = _read(root, f"{CPU_DIR}/cpu{cpu}/cpufreq/scaling_governor")
    if governor is None:
        out.append(Finding("governor", "unknown", f"cpu{cpu} has no cpufreq policy"))
    elif governor == "performance":
        out.append(Finding("governor", "ok", f"cpu{cpu} governor is performance"))
    else:
        out.append(Finding("governor", "fail", f"cpu{cpu} governor is {governor}, want performance"))

    boost = _read(root, f"{CPU_DIR}/cpufreq/boost")
    no_turbo = _read(root, f"{CPU_DIR}/intel_pstate/no_turbo")
    if boost is None and no_turbo is None:
        out.append(Finding("boost", "unknown", "no boost or no_turbo control is exposed"))
    elif boost == "1" or no_turbo == "0":
        out.append(Finding("boost", "fail", "frequency boost is allowed"))
    else:
        out.append(Finding("boost", "ok", "frequency boost is off"))

    # core_cpus_list is the current name; thread_siblings_list is its deprecated alias.
    siblings = _read(root, f"{CPU_DIR}/cpu{cpu}/topology/core_cpus_list")
    if siblings is None:
        siblings = _read(root, f"{CPU_DIR}/cpu{cpu}/topology/thread_siblings_list")
    if siblings is None:
        out.append(Finding("smt", "unknown", f"cpu{cpu} exposes no core_cpus_list"))
    else:
        online = _read(root, f"{CPU_DIR}/online")
        others = [c for c in parse_cpulist(siblings) if c != cpu]
        if online is not None:  # a sibling that is offline cannot run anything
            others = [c for c in others if c in parse_cpulist(online)]
        if others:
            out.append(Finding("smt", "fail", f"cpu{cpu} shares a core with online cpu {format_cpulist(others)}"))
        else:
            out.append(Finding("smt", "ok", f"cpu{cpu} has no online SMT sibling"))

    boot_isolated = parse_cpulist(_read(root, f"{CPU_DIR}/isolated") or "")
    cpuset_isolated = parse_cpulist(_read(root, "sys/fs/cgroup/cpuset.cpus.isolated") or "")
    if cpu in boot_isolated:
        out.append(Finding("isolation", "ok", f"cpu{cpu} is isolated at boot (isolcpus)"))
    elif cpu in cpuset_isolated:
        out.append(Finding("isolation", "ok", f"cpu{cpu} is in an isolated cpuset partition"))
    else:
        out.append(Finding("isolation", "fail", f"cpu{cpu} takes part in scheduler load balancing"))

    irq_dir = root / "proc/irq"
    irqs = sorted(p for p in irq_dir.glob("*/smp_affinity_list")) if irq_dir.is_dir() else []
    if not irqs:
        out.append(Finding("irq", "unknown", "no /proc/irq/N/smp_affinity_list is readable"))
    else:
        hits = sum(1 for p in irqs if cpu in parse_cpulist(p.read_text()))
        if hits:
            out.append(Finding("irq", "fail", f"{hits} of {len(irqs)} IRQs may be delivered to cpu{cpu}"))
        else:
            out.append(Finding("irq", "ok", f"0 of {len(irqs)} IRQs may be delivered to cpu{cpu}"))

    nohz = _read(root, f"{CPU_DIR}/nohz_full")
    if nohz is not None and cpu in parse_cpulist(nohz):
        out.append(Finding("tick", "ok", f"cpu{cpu} is in nohz_full"))
    else:
        out.append(Finding("tick", "note", f"cpu{cpu} is not in nohz_full, the scheduler tick still fires"))
    return out


def gate(findings: list[Finding]) -> str:
    """PASS: every required control verified. DEGRADED: some not exposed. FAIL: at least one is wrong."""
    statuses = {f.status for f in findings}
    if "fail" in statuses:
        return "FAIL"
    return "DEGRADED" if "unknown" in statuses else "PASS"


def pick_cpu(root: Path, allowed: list[int]) -> tuple[int, str]:
    """Prefer an isolated CPU we are allowed on; otherwise the highest allowed CPU (cpu0 takes the most IRQs)."""
    isolated = set(parse_cpulist(_read(root, f"{CPU_DIR}/isolated") or ""))
    isolated |= set(parse_cpulist(_read(root, "sys/fs/cgroup/cpuset.cpus.isolated") or ""))
    usable = sorted(isolated & set(allowed))
    if usable:
        return usable[-1], "isolated"
    return max(allowed), "not isolated"


# --------------------------------------------------------------------------- schedule


def interleaved_order(pairs: int, rng: random.Random) -> list[str]:
    """Randomized block design: each block holds one A and one B, in an order drawn per block."""
    order: list[str] = []
    for _ in range(pairs):
        order.extend(("A", "B") if rng.random() < 0.5 else ("B", "A"))
    return order


def sequential_order(pairs: int) -> list[str]:
    """What not to do: all of A, then all of B."""
    return ["A"] * pairs + ["B"] * pairs


def mean_position(order: list[str], variant: str) -> float:
    positions = [i for i, v in enumerate(order) if v == variant]
    return sum(positions) / len(positions)


# --------------------------------------------------------------------------- statistics


def u_statistic(a: list[float], b: list[float]) -> tuple[float, list[int]]:
    """U for sample a (pairs with a_i > b_j, ties counting one half) and the sizes of tied groups."""
    pooled = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    rank_sum_a = 0.0
    ties: list[int] = []
    i = 0
    while i < len(pooled):
        j = i
        while j < len(pooled) and pooled[j][0] == pooled[i][0]:
            j += 1
        midrank = (i + 1 + j) / 2  # average of the 1-based ranks i+1 .. j
        rank_sum_a += midrank * sum(1 for k in range(i, j) if pooled[k][1] == 0)
        if j - i > 1:
            ties.append(j - i)
        i = j
    return rank_sum_a - len(a) * (len(a) + 1) / 2, ties


@functools.cache
def u_null_counts(n1: int, n2: int) -> tuple[int, ...]:
    """Number of the C(n1+n2, n1) orderings that give each U in 0..n1*n2, when there are no ties.

    Recurrence over which sample owns the largest value: f(n1, n2, u) = f(n1-1, n2, u-n2) + f(n1, n2-1, u).
    """
    prev = [[1] for _ in range(n2 + 1)]  # n1 = 0: U is always 0
    for i in range(1, n1 + 1):
        cur = [[1]]  # n2 = 0
        for j in range(1, n2 + 1):
            size = i * j + 1
            row = [0] * size
            above, left = prev[j], cur[j - 1]
            for u in range(size):
                total = above[u - j] if 0 <= u - j < len(above) else 0
                if u < len(left):
                    total += left[u]
                row[u] = total
            cur.append(row)
        prev = cur
    return tuple(prev[n2])


def u_test(a: list[float], b: list[float], exact_limit: int = 1000) -> tuple[float, float, str]:
    """Two-sided Mann-Whitney U test. Returns (U for a, p-value, method).

    Exact when there are no ties and n1*n2 <= exact_limit; otherwise the normal approximation
    with tie correction and a continuity correction of one half.
    """
    n1, n2 = len(a), len(b)
    u_a, ties = u_statistic(a, b)
    u_min = min(u_a, n1 * n2 - u_a)
    if not ties and n1 * n2 <= exact_limit:
        counts = u_null_counts(n1, n2)
        tail = sum(counts[: int(u_min) + 1])
        return u_a, min(1.0, 2 * tail / sum(counts)), "exact"
    n = n1 + n2
    tie_term = sum(t**3 - t for t in ties) / (n * (n - 1))
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - tie_term))
    z = (n1 * n2 / 2 - u_min - 0.5) / sigma
    return u_a, min(1.0, math.erfc(max(z, 0.0) / math.sqrt(2))), "normal"


def hodges_lehmann(a: list[float], b: list[float]) -> float:
    """Median of all pairwise differences b_j - a_i: the shift estimate that matches the U test."""
    return statistics.median(y - x for x in a for y in b)


def cv_pct(values: list[float]) -> float:
    return 100 * statistics.stdev(values) / statistics.fmean(values)


def mad_pct(values: list[float]) -> float:
    med = statistics.median(values)
    return 100 * statistics.median(abs(v - med) for v in values) / med


class Comparison(NamedTuple):
    n_a: int
    n_b: int
    u_a: float
    p: float
    method: str
    shift_pct: float  # Hodges-Lehmann shift of B relative to median(A); positive means B is slower
    verdict: str


def compare(a: list[float], b: list[float], alpha: float = 0.05, min_effect_pct: float = 1.0) -> Comparison:
    u_a, p, method = u_test(a, b)
    shift = 100 * hodges_lehmann(a, b) / statistics.median(a)
    if p >= alpha:
        verdict = "no difference detected"
    elif abs(shift) < min_effect_pct:
        verdict = "significant but below the effect threshold"
    else:
        verdict = "B slower" if shift > 0 else "B faster"
    return Comparison(len(a), len(b), u_a, p, method, shift, verdict)


# --------------------------------------------------------------------------- runner


def can_disable_aslr() -> tuple[bool, str]:
    """Try personality(ADDR_NO_RANDOMIZE) in a throwaway child; seccomp profiles often forbid it."""
    libc = ctypes.CDLL(None, use_errno=True)
    pid = os.fork()
    if pid == 0:
        code = 0
        if libc.personality(ADDR_NO_RANDOMIZE) == -1:
            code = ctypes.get_errno() or 255
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code == 0, "permitted" if code == 0 else f"denied ({os.strerror(code)})"


def run_sample(cmd: list[str], cpu: int, disable_aslr: bool) -> dict[str, str]:
    """Run cmd in a fresh process confined to `cpu`, with a fixed empty environment.

    The affinity mask and the personality are set between fork and exec, and both survive execve.
    The empty environment fixes the environment block on the initial stack; the auxiliary vector still carries per-exec values such as the AT_RANDOM bytes.
    """
    libc = ctypes.CDLL(None, use_errno=True)

    def confine() -> None:
        os.sched_setaffinity(0, {cpu})
        if disable_aslr and libc.personality(ADDR_NO_RANDOMIZE) == -1:
            raise OSError(ctypes.get_errno(), "personality(ADDR_NO_RANDOMIZE) failed")

    done = subprocess.run(cmd, env={}, preexec_fn=confine, capture_output=True, text=True, check=True)
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def sample_ns(record: dict[str, str], summary: str = "min") -> float:
    """One value per process, from its timed passes.

    "min" suits noise that only ever adds time (preemption, interrupts, hypervisor steal).
    "median" keeps slow passes that belong to the workload itself (allocation, I/O, bimodal code).
    """
    passes = [int(v) for v in record["ns"].split(",")]
    return float(min(passes)) if summary == "min" else statistics.median(passes)


def run_ab(cmd_a: list[str], cmd_b: list[str], order: list[str], cpu: int, disable_aslr: bool):
    records: dict[str, list[dict[str, str]]] = {"A": [], "B": []}
    for variant in order:
        records[variant].append(run_sample(cmd_a if variant == "A" else cmd_b, cpu, disable_aslr))
    return records["A"], records["B"]
