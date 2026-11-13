"""Drive the low-noise harness and print facts that do not vary between runs.

stdout: derived facts only (verdicts, counts, exact p-values), diffed against expected/<platform>.txt.
stderr: this host's preflight findings and the measured timings, which differ on every run.

Usage: lab.py WORKDIR   (WORKDIR holds the compiled `workload` binary and is used for fixture trees)
"""

from __future__ import annotations

import math
import os
import random
import statistics
import sys
from fractions import Fraction
from pathlib import Path

import lownoise as ln

SEED = 20261109
PAIRS = 15
BASE_ITERS = 300_000
SLOW_ITERS = 390_000  # 30% more work, and the same number of digits so argv has the same size


def yes(flag: bool) -> str:
    return "yes" if flag else "no"


def signed_pct(value: float) -> str:
    """Two decimals with a sign; adding 0.0 turns a rounded -0.0 into +0.00."""
    return f"{round(value, 2) + 0.0:+.2f}%"


def note(text: str) -> None:
    print(text, file=sys.stderr)


# --------------------------------------------------------------------------- 1. preflight fixtures


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")


def fixture(governor: str, boost: str, online: str, isolated: str, nohz: str, irq_cpus: str) -> dict[str, str]:
    """An 8-CPU host with 4 cores: cpuN and cpuN+4 are SMT siblings, so cpu7 shares a core with cpu3."""
    cpu = ln.CPU_DIR
    files = {
        f"{cpu}/cpu7/cpufreq/scaling_governor": governor,
        f"{cpu}/cpufreq/boost": boost,
        f"{cpu}/cpu7/topology/core_cpus_list": "3,7",
        f"{cpu}/online": online,
        f"{cpu}/isolated": isolated,
        f"{cpu}/nohz_full": nohz,
    }
    for irq in (1, 9, 24, 25, 26, 27):
        files[f"proc/irq/{irq}/smp_affinity_list"] = irq_cpus
    return files


FIXTURES = {
    "default-desktop": fixture("schedutil", "1", "0-7", "", "", "0-7"),
    "tuned-host": fixture("performance", "0", "0-2,4-7", "3,7", "3,7", "0-2,4-6"),
    "tuned-but-sibling-online": fixture("performance", "0", "0-7", "3,7", "3,7", "0-2,4-6"),
}


def part_preflight(work: Path) -> None:
    print("== 1. Preflight gate on fixture hosts (benchmark CPU 7 of 8) ==")
    for name, files in FIXTURES.items():
        root = work / "fixtures" / name
        write_tree(root, files)
        findings = ln.preflight(root, 7)
        print(f"{name}: {ln.gate(findings)}")
        for f in findings:
            print(f"  [{f.status:<7}] {f.check}: {f.detail}")
    empty = work / "fixtures" / "nothing-exposed"
    empty.mkdir(parents=True)
    findings = ln.preflight(empty, 7)
    unknown = sum(1 for f in findings if f.status == "unknown")
    print(f"nothing-exposed: {ln.gate(findings)} ({unknown} controls unknown, isolation fails closed)")
    print(f"cpulist '0-2,6,10-11' parses to {ln.parse_cpulist('0-2,6,10-11')}")
    print()


# --------------------------------------------------------------------------- 2. schedule


def part_schedule() -> None:
    print(f"== 2. Schedule for {PAIRS} pairs (seed {SEED}) ==")
    inter = ln.interleaved_order(PAIRS, random.Random(SEED))
    seq = ln.sequential_order(PAIRS)
    blocks = ["".join(inter[i : i + 2]) for i in range(0, len(inter), 2)]
    print("sequential : " + "".join(seq))
    print("interleaved: " + " ".join(blocks))
    for name, order in (("sequential", seq), ("interleaved", inter)):
        gap = ln.mean_position(order, "B") - ln.mean_position(order, "A")
        print(f"{name}: A runs {order.count('A')}, B runs {order.count('B')}, mean position of B minus A = {gap:+.2f}")
    print(f"blocks that run A first: {blocks.count('AB')} of {PAIRS}")
    print()


# --------------------------------------------------------------------------- 3. U test


def part_u_test() -> None:
    print("== 3. Mann-Whitney U against published values ==")
    males, females = [19, 22, 16, 29, 24], [20, 11, 17, 12]
    u_m, p_exact, method = ln.u_test(males, females)
    print(f"SciPy documentation example: U_males = {u_m:g}, U_females = {5 * 4 - u_m:g}")
    print(f"  {method} two-sided p = {p_exact:.10f} (SciPy prints 0.1111111111111111)")
    _, p_normal, method = ln.u_test(males, females, exact_limit=0)
    print(f"  {method} two-sided p = {p_normal:.10f} (SciPy prints 0.11134688653314041)")

    counts = ln.u_null_counts(5, 4)
    print(f"null distribution for n=5,4: {sum(counts)} orderings (C(9,4) = {math.comb(9, 4)}), counts for U=0..3: {list(counts[:4])}")
    print(f"  exact p from those counts: 2 * {sum(counts[:4])} / {sum(counts)} = {Fraction(2 * sum(counts[:4]), sum(counts))}")

    for n in (3, 4, 5, 10, 15, 20):
        a = [float(i) for i in range(n)]
        b = [float(i + n) for i in range(n)]
        _, p, _ = ln.u_test(a, b)
        print(f"complete separation, n = {n:>2} + {n:<2}: smallest possible p = {p:.3g}")

    tied_a, tied_b = [1.0, 2.0, 2.0, 3.0, 5.0], [2.0, 4.0, 5.0, 6.0, 7.0]
    u_a, p, method = ln.u_test(tied_a, tied_b)
    print(f"ties force the {method} method: U_a = {u_a:g}, p = {p:.4f}")
    print(f"Hodges-Lehmann shift of [10, 11, 12] to [11, 13, 15]: {ln.hodges_lehmann([10, 11, 12], [11, 13, 15]):g}")
    print()


# --------------------------------------------------------------------------- 4. drift simulation

TRIALS = 200
SIM_PAIRS = 20
DRIFT_PCT = 3.0
NOISE_PCT = 0.3
SPIKE_PROBABILITY = 0.05


def gauss(rng: random.Random) -> float:
    """Box-Muller on random() only, so the stream is the same on every Python version."""
    return math.sqrt(-2 * math.log(1 - rng.random())) * math.cos(2 * math.pi * rng.random())


def simulate(order: list[str], rng: random.Random, effect_b_pct: float, drift_pct: float) -> tuple[list[float], list[float]]:
    """Synthetic timings: a linear upward drift over the session, Gaussian noise, and rare upward spikes."""
    a: list[float] = []
    b: list[float] = []
    for i, variant in enumerate(order):
        t = 100.0 * (1 + drift_pct / 100 * i / (len(order) - 1))
        if variant == "B":
            t *= 1 + effect_b_pct / 100
        t *= 1 + NOISE_PCT / 100 * gauss(rng)
        if rng.random() < SPIKE_PROBABILITY:
            t *= 1 + (1 + 2 * rng.random()) / 100
        (a if variant == "A" else b).append(t)
    return a, b


def part_drift() -> None:
    print(f"== 4. Drift simulation: {TRIALS} sessions of {SIM_PAIRS}+{SIM_PAIRS} samples per scenario ==")
    print(f"noise {NOISE_PCT:g}% Gaussian plus a 1-3% spike on {SPIKE_PROBABILITY:.0%} of samples; alpha 0.05, effect threshold 0.5%")
    scenarios = (
        ("A and B identical, no drift", 0.0, 0.0),
        (f"A and B identical, {DRIFT_PCT:g}% upward drift over the session", 0.0, DRIFT_PCT),
        (f"B truly 1% faster, {DRIFT_PCT:g}% upward drift over the session", -1.0, DRIFT_PCT),
    )
    for label, effect, drift in scenarios:
        print(f"{label}:")
        for schedule in ("sequential", "interleaved"):
            verdicts: dict[str, int] = {}
            shifts = []
            for trial in range(TRIALS):
                rng = random.Random(trial)
                order = ln.sequential_order(SIM_PAIRS) if schedule == "sequential" else ln.interleaved_order(SIM_PAIRS, rng)
                a, b = simulate(order, rng, effect, drift)
                result = ln.compare(a, b, alpha=0.05, min_effect_pct=0.5)
                verdicts[result.verdict] = verdicts.get(result.verdict, 0) + 1
                shifts.append(result.shift_pct)
            slower, faster = verdicts.get("B slower", 0), verdicts.get("B faster", 0)
            none = verdicts.get("no difference detected", 0)
            small = verdicts.get("significant but below the effect threshold", 0)
            print(
                f"  {schedule:<11}: B slower {slower:>3}, B faster {faster:>3}, below threshold {small:>3}, "
                f"no difference {none:>3}; median estimated shift {signed_pct(statistics.median(shifts))}"
            )
    print()


# --------------------------------------------------------------------------- 5. live run


def describe(tag: str, a: list[float], b: list[float], result: ln.Comparison) -> None:
    note(
        f"[live] {tag}: median A {statistics.median(a) / 1e6:.3f} ms, median B {statistics.median(b) / 1e6:.3f} ms, "
        f"CV A {ln.cv_pct(a):.2f}%, MAD A {ln.mad_pct(a):.2f}%, shift {result.shift_pct:+.2f}%, "
        f"p = {result.p:.3g} ({result.method}), U_a = {result.u_a:g} -> {result.verdict}"
    )


def part_live(work: Path) -> None:
    print("== 5. Live A/A and A/B run on this host (timings are on stderr) ==")
    root = Path("/")
    allowed = sorted(os.sched_getaffinity(0))
    cpu, how = ln.pick_cpu(root, allowed)
    aslr_permitted, aslr_reason = ln.can_disable_aslr()
    global_aslr = (root / "proc/sys/kernel/randomize_va_space").read_text().strip()
    aslr_off = aslr_permitted or global_aslr == "0"
    findings = ln.preflight(root, cpu)
    note(f"[live] benchmark cpu{cpu} ({how}), allowed CPUs {allowed[0]}-{allowed[-1]} ({len(allowed)} total)")
    note(f"[live] personality(ADDR_NO_RANDOMIZE): {aslr_reason}; randomize_va_space = {global_aslr}")
    note(f"[live] preflight gate: {ln.gate(findings)}")
    for f in findings:
        note(f"[live]   [{f.status:<7}] {f.check}: {f.detail}")

    workload = str(work / "workload")
    base, slow = [workload, str(BASE_ITERS)], [workload, str(SLOW_ITERS)]
    rng = random.Random(SEED)
    aa_a, aa_b = ln.run_ab(base, base, ln.interleaved_order(PAIRS, rng), cpu, aslr_permitted)
    ab_a, ab_b = ln.run_ab(base, slow, ln.interleaved_order(PAIRS, rng), cpu, aslr_permitted)
    records = aa_a + aa_b + ab_a + ab_b

    aa_times = [ln.sample_ns(r) for r in aa_a], [ln.sample_ns(r) for r in aa_b]
    ab_times = [ln.sample_ns(r) for r in ab_a], [ln.sample_ns(r) for r in ab_b]
    aa = ln.compare(*aa_times, alpha=0.05, min_effect_pct=1.0)
    ab = ln.compare(*ab_times, alpha=0.05, min_effect_pct=1.0)
    describe("A/A", *aa_times, aa)
    describe("A/B", *ab_times, ab)
    spread = [max(v) / min(v) for v in ([int(x) for x in r["ns"].split(",")] for r in records)]
    note(f"[live] slowest pass over fastest pass within one process: median {statistics.median(spread):.3f}, worst {max(spread):.3f}")
    switches = sum(int(r["nivcsw"]) for r in records)
    note(f"[live] involuntary context switches inside timed regions: {switches} over {len(records)} processes")

    base_sums = {r["checksum"] for r in aa_a + aa_b + ab_a}
    slow_sums = {r["checksum"] for r in ab_b}
    stacks = {r["stack"] for r in records}
    flags_match = all(bool(int(r["personality"], 16) & ln.ADDR_NO_RANDOMIZE) == aslr_permitted for r in records)
    note(f"[live] distinct stack addresses over {len(records)} launches: {len(stacks)}")

    print(f"samples collected: A/A {aa.n_a}+{aa.n_b}, A/B {ab.n_a}+{ab.n_b}, one fresh process each")
    print(f"every process was confined to exactly one CPU: {yes(all(r['allowed_cpus'] == '1' for r in records))}")
    on_cpu = all(int(r["cpu_start"]) == cpu and int(r["cpu_end"]) == cpu for r in records)
    print(f"every process started and ended its timed region on the chosen CPU: {yes(on_cpu)}")
    stable = len(base_sums) == 1 and len(slow_sums) == 1 and base_sums != slow_sums
    print(f"workload checksums: identical within a variant and different between variants: {yes(stable)}")
    print(f"ADDR_NO_RANDOMIZE flag in every child matches what the harness requested: {yes(flags_match)}")
    print(f"recorded ASLR state agrees with observed stack addresses (one address only when ASLR is off): {yes((len(stacks) == 1) == aslr_off)}")
    print(f"A/B verdict: {ab.verdict}; p < 0.01: {yes(ab.p < 0.01)}; shift above +15%: {yes(ab.shift_pct > 15)}")
    print(f"A/A shift is smaller in magnitude than the A/B shift: {yes(abs(aa.shift_pct) < abs(ab.shift_pct))}")
    record = {
        "cpu": cpu,
        "cpu_isolation": how,
        "aslr": "off" if aslr_off else "randomized",
        "gate": ln.gate(findings),
        "seed": SEED,
        "pairs": PAIRS,
        "alpha": 0.05,
        "min_effect_pct": 1.0,
        "aa_cv_pct": ln.cv_pct(aa_times[0]),
        "aa_shift_pct": aa.shift_pct,
        "ab_shift_pct": ab.shift_pct,
        "ab_p": ab.p,
        "kernel": os.uname().release,
    }
    print("result record fields: " + ", ".join(sorted(record)))


def main() -> int:
    work = Path(sys.argv[1])
    part_preflight(work)
    part_schedule()
    part_u_test()
    part_drift()
    part_live(work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
