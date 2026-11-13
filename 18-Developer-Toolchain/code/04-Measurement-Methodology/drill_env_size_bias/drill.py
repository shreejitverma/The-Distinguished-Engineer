#!/usr/bin/env python3
"""Drive the environment-size drill: reproduce the bias, diagnose it, and remove it from the result.

Usage: drill.py <path to the built kernel binary> [--raw]

Stdout carries only derived facts (counts and yes/no verdicts), so a recording is stable across runs.
--raw adds the per-position numbers on stderr, which are specific to the machine and the run.
Exit status: 0 when every check passes, 2 when the host cannot run the drill (the kernel binary says why),
3 when the drill ran and a check failed, which includes a CPU that shows no alignment-dependent swing.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
from dataclasses import dataclass

POSITIONS = 256  # stack positions in one 4096-byte period
STEP = 16  # bytes: the kernel rounds the initial stack pointer down to a multiple of 16
PAGE = POSITIONS * STEP
SLOW_RATIO = 1.15  # a position is slow when it costs 15% more cycles than the median position
MEDIAN_TOLERANCE = 0.02  # how closely the stabilized result must agree across environments
RUN_TIMEOUT_S = 300
FRAME_OFFSETS = (0, 4)  # g and inc, relative to the reported slot
STATIC_OFFSETS = (0, 4, 8)  # i, j and k, relative to the reported static address


@dataclass(frozen=True)
class Sample:
    static: int  # low 12 bits of the address of the static counter i
    slot: int  # low 12 bits of the address of the stack local g
    cycles: int
    instructions: int
    l1d_miss: int  # -1 when the CPU offers no such counter
    alias: int  # -1 when the CPU offers no such counter

    def aliased(self) -> bool:
        """True when a stack local and a static counter share their low 12 address bits."""
        statics = {(self.static + o) % PAGE for o in STATIC_OFFSETS}
        return any((self.slot + o) % PAGE in statics for o in FRAME_OFFSETS)


@dataclass(frozen=True)
class Run:
    iters: int
    alias_event: str
    samples: list[Sample]


def run_kernel(exe: str, mode: str, pad_bytes: int) -> Run:
    """Run the kernel with an environment that holds exactly one variable of the given size."""
    env = {"PAD": "x" * pad_bytes}
    proc = subprocess.run(["kernel", mode], executable=exe, env=env, capture_output=True, text=True,
                          check=False, timeout=RUN_TIMEOUT_S)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode if proc.returncode > 0 else 2)
    meta: dict[str, str] = {}
    samples = []
    for line in proc.stdout.splitlines():
        kind, *fields = line.split()
        pairs = dict(f.split("=", 1) for f in fields)
        if kind == "meta":
            meta = pairs
        elif kind == "sample":
            samples.append(Sample(static=int(pairs["static"], 16), slot=int(pairs["slot"], 16),
                                  cycles=int(pairs["cycles"]), instructions=int(pairs["instructions"]),
                                  l1d_miss=int(pairs["l1d_miss"]), alias=int(pairs["alias"])))
    expected = POSITIONS if mode == "sweep" else 1
    if not meta or len(samples) != expected:
        sys.exit(f"drill: kernel {mode} printed {len(samples)} samples, expected {expected}")
    return Run(iters=int(meta["iters"]), alias_event=meta["alias_event"], samples=samples)


def yes(flag: bool) -> str:
    return "yes" if flag else "no"


def slow_positions(samples: list[Sample]) -> set[int]:
    median = statistics.median(s.cycles for s in samples)
    return {n for n, s in enumerate(samples) if s.cycles > SLOW_RATIO * median}


def raw_table(title: str, iters: int, samples: list[Sample]) -> None:
    print(f"# {title}", file=sys.stderr)
    print("# position slot cycles/iter instructions/iter l1d_miss/1000iter alias/iter aliased", file=sys.stderr)
    for n, s in enumerate(samples):
        l1d = "-" if s.l1d_miss < 0 else f"{1000 * s.l1d_miss / iters:.3f}"
        alias = "-" if s.alias < 0 else f"{s.alias / iters:.3f}"
        print(f"{n:4d} {s.slot:03x} {s.cycles / iters:8.3f} {s.instructions / iters:8.3f} {l1d:>8} {alias:>8} "
              f"{yes(s.aliased())}", file=sys.stderr)
    cycles = [s.cycles for s in samples]
    print(f"# slowest / median cycles per iteration: {max(cycles) / statistics.median(cycles):.3f}", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.exit(__doc__)
    exe, raw = argv[1], "--raw" in argv[2:]
    checks: list[bool] = []

    def check(flag: bool) -> str:
        checks.append(flag)
        return yes(flag)

    # Step 1: the same binary and argv, with only the environment growing 16 bytes at a time.
    runs = [run_kernel(exe, "naive", n * STEP) for n in range(POSITIONS)]
    iters, alias_event = runs[0].iters, runs[0].alias_event
    naive = [r.samples[0] for r in runs]
    if raw:
        raw_table("naive: position n is an environment padded by 16 * n bytes", iters, naive)
    moved = all((naive[n].slot - naive[n + 1].slot) % PAGE == STEP for n in range(POSITIONS - 1))
    ipi = [s.instructions / iters for s in naive]
    slow = slow_positions(naive)
    print("== Reproduce: one binary, one argv, 256 environment sizes, ASLR off ==")
    print(f"environment padding swept: 0 to {(POSITIONS - 1) * STEP} bytes in {STEP}-byte steps")
    print(f"stack frame moved down {STEP} bytes for every {STEP} bytes of environment: {check(moved)}")
    print(f"distinct page offsets visited by the frame: {len({s.slot for s in naive})} of {POSITIONS}")
    print(f"instructions per iteration: {statistics.median(ipi):.2f}, the same at every position within 0.1%: "
          f"{check(max(ipi) - min(ipi) < 0.001 * min(ipi))}")
    print(f"positions at least {round((SLOW_RATIO - 1) * 100)}% slower than the median, in cycles per iteration: "
          f"{len(slow)} of {POSITIONS}")
    checks.append(bool(slow))

    # Step 2: what the counters say about the slow positions.
    aliased = {n for n, s in enumerate(naive) if s.aliased()}
    print("== Diagnose: what differs at the slow positions ==")
    if all(s.l1d_miss >= 0 for s in naive):
        quiet = all(1000 * s.l1d_miss < iters for s in naive)
        print(f"L1D load misses stay under 1 per 1000 iterations at every position: {check(quiet)}")
    else:
        print("L1D load misses: counter not offered by this CPU")
    print(f"positions where a stack local and a static share their low 12 address bits: {len(aliased)} of {POSITIONS}")
    print(f"the slow positions are exactly those positions: {check(slow == aliased)}")
    if alias_event != "none":
        hot = all(naive[n].alias >= 0.5 * iters for n in aliased)
        cold = all(s.alias < 0.01 * iters for n, s in enumerate(naive) if n not in aliased)
        print(f"{alias_event} per iteration is at least 0.5 at those positions and under 0.01 elsewhere: "
              f"{check(hot and cold)}")
    else:
        print("load-blocked-by-alias counter: no known event for this CPU model")

    # Step 3: the harness sweeps the stack offset itself, so the answer stops depending on the environment.
    worst = max(range(POSITIONS), key=lambda n: naive[n].cycles)
    best = min(range(POSITIONS), key=lambda n: naive[n].cycles)
    chosen = [worst, best, (worst + POSITIONS // 2) % POSITIONS]
    swing = naive[worst].cycles / naive[best].cycles
    sweeps = [run_kernel(exe, "sweep", n * STEP).samples for n in chosen]
    if raw:
        for n, samples in zip(chosen, sweeps):
            raw_table(f"sweep inside an environment padded by {n * STEP} bytes", iters, samples)
    medians = [statistics.median(s.cycles for s in samples) for samples in sweeps]
    slow_counts = [len(slow_positions(samples)) for samples in sweeps]
    covered = all(len({s.slot for s in samples}) == POSITIONS for samples in sweeps)
    same_set = all({s.slot for s in samples if s.aliased()} ==
                   {samples[n].slot for n in slow_positions(samples)} for samples in sweeps)
    print("== Stabilize: sweep the stack offset inside the harness ==")
    print("environments compared: 3 (the slowest, the fastest, and one 2048 bytes away from the slowest)")
    print(f"naive result differs by at least {round((SLOW_RATIO - 1) * 100)}% between them: "
          f"{check(swing >= SLOW_RATIO)}")
    print(f"each in-process sweep visited {POSITIONS} distinct page offsets: {check(covered)}")
    print(f"slow offsets found by each sweep: {', '.join(str(c) for c in slow_counts)} of {POSITIONS}")
    print(f"each sweep flags exactly its address-aliased offsets: {check(same_set)}")
    print(f"median cycles per iteration agrees across the 3 environments within "
          f"{round(MEDIAN_TOLERANCE * 100)}%: {check(max(medians) - min(medians) < MEDIAN_TOLERANCE * min(medians))}")

    passed = all(checks)
    print("== Verdict ==")
    print(f"bias reproduced, explained by address aliasing, and removed from the reported result: {yes(passed)}")
    if not passed:
        print("drill: at least one check failed; rerun with DRILL_RAW=1 for the per-position numbers. "
              "A CPU without a 4K-aliasing penalty on this loop shows no slow position.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
