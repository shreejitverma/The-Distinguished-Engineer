"""Check the mini profiler's output against the workload's known answer.

Sample counts differ from run to run, so nothing here prints a raw count. Each line is a
derived fact that is the same on every run when the mechanism works and different when it
does not: a share inside a tolerance band, a frame that must or must not exist, an address
that must equal the one `nm` reports.

usage: report.py main WORKDIR     full report for run.sh
       report.py perf WORKDIR     report for perf_ring/run.sh
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BURN = "(anonymous namespace)::burn(unsigned int)"
DECODE = "pipeline::decode(unsigned int)"
MATCH = "pipeline::match(unsigned int)"
REPORT = "pipeline::report(unsigned int, unsigned int)"
VIA_TAILCALL = "pipeline::via_tailcall(unsigned int)"
VIA_INLINE = "pipeline::via_inline(unsigned int)"
ROUND = "run_round(bool)"

# Stack below main -> expected share of all samples, in percent. Fixed by the unit counts in workload.cpp.
PIPELINE = {
    (ROUND, DECODE, BURN): 50,
    (ROUND, MATCH, BURN): 30,
    (ROUND, REPORT, REPORT, REPORT, BURN): 20,
}
# A tail call replaces via_tailcall's frame and inlining leaves inlined_helper without one.
PITFALLS = {
    (ROUND, BURN): 60,
    (ROUND, VIA_INLINE, BURN): 40,
}
# Without a frame record in the leaf, the walk skips the leaf's direct caller.
LEAFLESS = {
    (ROUND, BURN): 80,
    (ROUND, REPORT, REPORT, BURN): 20,
}
MIN_SHARE = 3.0  # percent; stacks below this are sampling noise (a sample in a prologue, in main)


def read_folded(path: Path) -> dict[tuple[str, ...], int]:
    """Parse folded stacks: frames joined by ';', a space, then the sample count."""
    stacks: dict[tuple[str, ...], int] = {}
    for line in path.read_text().splitlines():
        stack, _, count = line.rpartition(" ")
        stacks[tuple(stack.split(";"))] = stacks.get(tuple(stack.split(";")), 0) + int(count)
    return stacks


def read_kv(path: Path, sep: str) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        key, found, value = line.partition(sep)
        if found and key not in ("map", "sample"):
            out[key] = value
    return out


def below_main(stack: tuple[str, ...]) -> tuple[str, ...] | None:
    return stack[stack.index("main") + 1 :] if "main" in stack else None


def yes(flag: bool) -> str:
    return "yes" if flag else "no"


def check_shares(stacks: dict[tuple[str, ...], int], expected: dict[tuple[str, ...], int], tolerance: int) -> None:
    total = sum(stacks.values())
    seen = set()
    for stack, count in sorted(stacks.items(), key=lambda kv: (-kv[1], kv[0])):
        share = 100.0 * count / total
        if share < MIN_SHARE:
            continue
        print(f"  {';'.join(stack)}")
        want = expected.get(below_main(stack) or ())
        if want is None:
            print("    not a stack the workload can produce")
        else:
            seen.add(below_main(stack))
            print(f"    expected {want}% of samples, measured within {tolerance} points: {yes(abs(share - want) <= tolerance)}")
    print(f"  expected stacks missing: {len(set(expected) - seen)}")


def count_frames(stacks: dict[tuple[str, ...], int], needle: str) -> int:
    return sum(count for stack, count in stacks.items() if any(needle in frame for frame in stack))


def share_with(stacks: dict[tuple[str, ...], int], needle: str) -> float:
    return 100.0 * count_frames(stacks, needle) / sum(stacks.values())


def dominant_leaf(stacks: dict[tuple[str, ...], int]) -> str:
    leaves: dict[str, int] = {}
    for stack, count in stacks.items():
        leaves[stack[-1]] = leaves.get(stack[-1], 0) + count
    return max(sorted(leaves), key=lambda name: leaves[name])


def nm_agreement(work: Path) -> str:
    """Compare the lab's symbol-table parser with nm for the workload's functions."""
    wanted = ("burn", "decode", "match", "report", "via_tailcall", "via_inline", "run_round")
    truth: dict[str, int] = {}
    for line in subprocess.run(["nm", str(work / "target")], capture_output=True, text=True, check=True).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in "Tt" and (parts[2].lstrip("_") == "main" or any(w in parts[2] for w in wanted)):
            truth[parts[2]] = int(parts[0], 16)
    ours: dict[str, int] = {}
    for line in (work / "target.symbols").read_text().splitlines():
        addr, _, name = line.partition(" ")
        ours[name] = int(addr, 16)
    same = sum(1 for name, addr in truth.items() if ours.get(name) == addr)
    return f"{same} of {len(truth)}"


def report_main(work: Path) -> None:
    raw = read_kv(work / "pipeline.raw", " ")
    stats = read_kv(work / "pipeline.stats", "=")
    pipeline = read_folded(work / "pipeline.folded")
    total = sum(pipeline.values())
    requested = int((work / "requested").read_text())
    cpu_us, interval_us = int(raw["cpu_us"]), int(raw["interval_us"])
    # Timer granularity is a property of the host kernel, so it goes to stderr, not into the compared output.
    print(f"info: {total} samples in {cpu_us} us of CPU time, {cpu_us / total:.0f} us per sample "
          f"(requested {interval_us} us)", file=sys.stderr)

    print("== 1. Sample: ITIMER_PROF, SIGPROF, frame-pointer walk ==")
    print(f"samples requested: {requested}")
    print(f"samples captured, at least the number requested: {yes(total >= requested)}")
    print(f"samples dropped because the buffer was full: {raw['dropped']}")
    print(f"stacks cut at the depth limit: {stats['truncated']}")
    rooted = sum(count for stack, count in pipeline.items() if "main" in stack)
    print(f"stacks that reach main, at least 99%: {yes(100.0 * rooted / total >= 99.0)}")

    print()
    print("== 2. Symbolize: the executable's own symbol table ==")
    identity = {"ELF64": "GNU build ID", "Mach-O 64": "LC_UUID"}.get(stats["format"], "none")
    print(f"image format: {stats['format']}")
    print(f"symbol source: {stats['symbol_source']}")
    print(f"identity checked: {identity}, sampled process and file on disk: {stats['identity']}")
    print(f"workload symbols at the address nm reports: {nm_agreement(work)}")
    resolved = 100.0 * int(stats["main_resolved"]) / int(stats["main_frames"])
    print(f"frames inside the executable resolved to a symbol name, at least 99%: {yes(resolved >= 99.0)}")

    print()
    print("== 3. Fold: stacks holding at least 3% of samples, heaviest first ==")
    check_shares(pipeline, PIPELINE, 6)
    leaf = 100.0 * sum(count for stack, count in pipeline.items() if stack[-1] == BURN) / total
    print(f"flat view, samples whose leaf frame is burn, at least 99%: {yes(leaf >= 99.0)}")

    print()
    print("== 4. Frames that never existed: tail call and inlining ==")
    pitfalls = read_folded(work / "pitfalls.folded")
    check_shares(pitfalls, PITFALLS, 8)
    print(f"  samples with a via_tailcall frame, fewer than 1%: {yes(share_with(pitfalls, 'via_tailcall') < 1.0)}")
    print(f"  samples with an inlined_helper frame: {count_frames(pitfalls, 'inlined_helper')}")

    print()
    print("== 5. Leaf without a frame record: the caller disappears ==")
    leafless = read_folded(work / "leafless.folded")
    check_shares(leafless, LEAFLESS, 8)
    callers = share_with(leafless, "decode") + share_with(leafless, "match")
    print(f"  samples with a decode or match frame, fewer than 1%: {yes(callers < 1.0)}")

    print()
    print("== 6. Damaged or wrong symbol tables ==")
    strip_x = read_kv(work / "strip_x.stats", "=")
    print(f"strip -x copy, identity still equals the sampled build: {yes(strip_x['identity'] == 'match')}")
    print(f"  bounded lookup names the hot leaf: {dominant_leaf(read_folded(work / 'strip_x_bounded.folded'))}")
    print(f"  nearest-symbol lookup names the hot leaf: {dominant_leaf(read_folded(work / 'strip_x_nearest.folded'))}")
    stripped = read_kv(work / "stripped.stats", "=")
    print(f"fully stripped copy, symbol source: {stripped['symbol_source']}")
    unnamed = int(stripped["main_frames"]) > 0 and int(stripped["main_resolved"]) == 0
    print(f"  frames inside the executable resolved to a symbol name: {'none' if unnamed else 'some'}")
    print(f"different build of the same source, symbolizer exit status: {(work / 'other.status').read_text().strip()}")


def report_perf(work: Path) -> None:
    raw = read_kv(work / "perf.raw", " ")
    stats = read_kv(work / "perf.stats", "=")
    perf = read_folded(work / "perf.folded")
    total = sum(perf.values())
    print(f"info: {total} samples, {raw.get('lost', '0')} lost", file=sys.stderr)
    print("== perf_event_open front end: cpu-clock samples with kernel-built callchains ==")
    print(f"samples read from the ring buffer, at least 500: {yes(total >= 500)}")
    print(f"PERF_RECORD_MMAP2 records that map the target executable, at least 1: {yes(int(raw['exec_maps']) >= 1)}")
    print(f"PERF_RECORD_EXIT seen for the target: {yes(raw['exit_seen'] == '1')}")
    print(f"image format: {stats['format']}")
    print(f"symbol source: {stats['symbol_source']}")
    print(f"identity checked: GNU build ID from PERF_RECORD_MMAP2 and file on disk: {stats['identity']}")
    rooted = sum(count for stack, count in perf.items() if "main" in stack)
    print(f"stacks that reach main, at least 99%: {yes(100.0 * rooted / total >= 99.0)}")
    print("stacks holding at least 3% of samples, heaviest first:")
    check_shares(perf, PIPELINE, 8)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("main", "perf"):
        print(__doc__, file=sys.stderr)
        return 2
    (report_main if argv[1] == "main" else report_perf)(Path(argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
