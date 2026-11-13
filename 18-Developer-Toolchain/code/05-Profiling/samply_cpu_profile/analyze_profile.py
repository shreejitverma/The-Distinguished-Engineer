"""Read a samply profile and its symbol sidecar, and check the profile against the workload.

samply writes the Firefox Profiler "processed profile" JSON. Each thread carries column tables:
samples (stack, time, weight, threadCPUDelta), stackTable (prefix, frame), frameTable (address,
func), funcTable (resource), resourceTable (lib). With `--save-only` the addresses are not
symbolicated; `--unstable-presymbolicate` adds a `<profile>.syms.json` sidecar that names every
sampled address. This script joins the two and derives self and inclusive cost per function.

Times and sample counts change from run to run, so the report prints them on stderr and prints
only the pass or fail of each banded check on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


class ProfileError(Exception):
    """The profile or its sidecar does not have the shape this lab relies on."""


def short_name(symbol: str) -> str:
    """Strip the parameter list and template arguments: 'chase(unsigned int const*, ...)' -> 'chase'."""
    for stop in ("(", "<", "["):
        cut = symbol.find(stop)
        if cut > 0:
            symbol = symbol[:cut]
    return symbol.rsplit("::", 1)[-1]


class Sidecar:
    """The .syms.json file: per library, sampled address -> symbol, plus debug-info frames."""

    def __init__(self, path: Path) -> None:
        doc = json.loads(path.read_text())
        self.strings: list[str] = doc["string_table"]
        self.by_lib: dict[str, dict[int, dict]] = {}
        for lib in doc["data"]:
            table = lib["symbol_table"]
            self.by_lib[lib["debug_name"]] = {rva: table[index] for rva, index in lib["known_addresses"]}

    def entry(self, lib: str, address: int) -> dict | None:
        return self.by_lib.get(lib, {}).get(address)

    def symbol(self, lib: str, address: int) -> str:
        entry = self.entry(lib, address)
        return short_name(self.strings[entry["symbol"]]) if entry else f"{lib}+{address:#x}"

    def debug_frames(self, lib: str, symbol: str) -> list[str] | None:
        """Names from debug info for a symbol, innermost inlined function first; None without debug info."""
        for entry in self.by_lib.get(lib, {}).values():
            if short_name(self.strings[entry["symbol"]]) == symbol:
                frames = entry.get("frames")
                if frames is None:
                    return None
                return [short_name(self.strings[f["function"]]) for f in frames if f.get("function") is not None]
        raise ProfileError(f"no sampled address resolved to {symbol!r} in {lib!r}")


class ThreadProfile:
    """One thread of the profile with its stacks resolved to symbol names, root first."""

    def __init__(self, profile: dict, thread: dict, sidecar: Sidecar) -> None:
        libs = [lib["debugName"] for lib in profile["libs"]]
        frames, funcs, resources = thread["frameTable"], thread["funcTable"], thread["resourceTable"]
        stacks, samples = thread["stackTable"], thread["samples"]

        def frame_name(frame: int) -> str:
            resource = funcs["resource"][frames["func"][frame]]
            lib_index = resources["lib"][resource] if resource is not None and resource >= 0 else None
            lib = libs[lib_index] if lib_index is not None else "?"
            return sidecar.symbol(lib, frames["address"][frame])

        names = [frame_name(i) for i in range(frames["length"])]
        resolved: list[tuple[str, ...]] = []
        for i in range(stacks["length"]):  # a prefix always has a smaller index than its stack
            prefix = stacks["prefix"][i]
            base = resolved[prefix] if prefix is not None else ()
            resolved.append(base + (names[stacks["frame"][i]],))

        self.rows: list[tuple[tuple[str, ...], int, int]] = []  # (stack, ticks, cpu microseconds)
        weights = samples.get("weight") or [1] * samples["length"]
        for i in range(samples["length"]):
            stack = samples["stack"][i]
            if stack is None:
                continue
            self.rows.append((resolved[stack], weights[i], samples["threadCPUDelta"][i] or 0))
        self.times = samples["time"]

    def totals(self) -> tuple[int, int]:
        return sum(r[1] for r in self.rows), sum(r[2] for r in self.rows)

    def self_cost(self) -> tuple[dict[str, int], dict[str, int]]:
        ticks: dict[str, int] = defaultdict(int)
        cpu: dict[str, int] = defaultdict(int)
        for stack, weight, cpu_us in self.rows:
            ticks[stack[-1]] += weight
            cpu[stack[-1]] += cpu_us
        return ticks, cpu

    def inclusive_cost(self) -> tuple[dict[str, int], dict[str, int]]:
        ticks: dict[str, int] = defaultdict(int)
        cpu: dict[str, int] = defaultdict(int)
        for stack, weight, cpu_us in self.rows:
            for name in set(stack):  # count a recursive function once per sample
                ticks[name] += weight
                cpu[name] += cpu_us
        return ticks, cpu

    def cost_under(self, parent: str, leaf: str) -> tuple[int, int]:
        """Cost of samples whose innermost frame is `leaf` and whose stack contains `parent`."""
        ticks = cpu = 0
        for stack, weight, cpu_us in self.rows:
            if stack[-1] == leaf and parent in stack:
                ticks += weight
                cpu += cpu_us
        return ticks, cpu


def load(profile_path: Path) -> tuple[dict, Sidecar]:
    sidecar_path = profile_path.with_suffix(".syms.json")
    if not sidecar_path.exists():
        raise ProfileError(f"missing sidecar {sidecar_path.name}: record with --unstable-presymbolicate")
    return json.loads(profile_path.read_text()), Sidecar(sidecar_path)


def main_thread(profile: dict, sidecar: Sidecar) -> ThreadProfile:
    threads = [t for t in profile["threads"] if t.get("isMainThread")]
    if len(threads) != 1:
        raise ProfileError(f"expected one main thread, found {len(threads)}")
    return ThreadProfile(profile, threads[0], sidecar)


class Phase:
    """One phase as the workload measured it: wall and thread CPU time, in milliseconds."""

    def __init__(self, wall_ns: str, cpu_ns: str) -> None:
        self.wall_ms = int(wall_ns) / 1e6
        self.cpu_ms = int(cpu_ns) / 1e6


def read_timings(path: Path) -> dict[str, Phase]:
    timings = {}
    for line in path.read_text().splitlines():
        name, wall_ns, cpu_ns = line.split()
        timings[name] = Phase(wall_ns, cpu_ns)
    return timings


def verdict(ok: bool) -> str:
    return "yes" if ok else "NO"


def within(value: float, reference: float, tolerance: float) -> bool:
    return abs(value - reference) <= tolerance * reference


IN_ORDER_HOPS = 100_000_000  # kInOrderHops in kernels.cpp
SCATTERED_HOPS = 1_500_000  # kScatteredHops in kernels.cpp


def report_full(profile: dict, sidecar: Sidecar, timings: dict[str, Phase], binary: str) -> bool:
    thread = main_thread(profile, sidecar)
    meta = profile["meta"]
    total_ticks, total_cpu_us = thread.totals()
    self_ticks, self_cpu = thread.self_cost()
    incl_ticks, incl_cpu = thread.inclusive_cost()
    span_ms = thread.times[-1] - thread.times[0]
    ok = True

    def check(label: str, passed: bool) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  {label}: {verdict(passed)}")

    def note(text: str) -> None:  # run-dependent numbers: visible when run by hand, not recorded
        print(f"    [{text}]", file=sys.stderr)

    print("Profile file")
    print(f"  format: Firefox Profiler processed profile, version {meta['preprocessedProfileVersion']}, "
          f"sampling interval {meta['interval']:g} ms")
    print(f"  symbolicated in the JSON: {str(meta['symbolicated']).lower()} (names come from the sidecar)")
    print(f"  threads recorded: {len(profile['threads'])}")
    columns = sorted(k for k, v in profile["threads"][0]["samples"].items() if isinstance(v, list))
    print(f"  sample columns: {', '.join(columns)}")
    note(f"{len(thread.rows)} samples, {total_ticks} ticks over {span_ms:.0f} ms, {total_cpu_us / 1000:.0f} ms of CPU")
    # Suspending a thread 1000 times a second is best effort: a late wakeup stretches a tick.
    check("ticks recorded are 40% to 105% of one per interval", 0.40 * span_ms <= total_ticks <= 1.05 * span_ms)

    print("Self cost by function (innermost frame), every function with at least 5% of CPU time")
    hot = sorted(n for n in self_cpu if self_cpu[n] * 20 >= total_cpu_us)
    print(f"  {', '.join(hot)}")
    for name in sorted(hot, key=lambda n: -self_cpu[n]):
        note(f"{name}: {self_cpu[name] / 1000:.0f} ms CPU, {self_ticks[name]} ticks")
    check("sum_serial has more self CPU time than sum_unrolled4", self_cpu["sum_serial"] > self_cpu["sum_unrolled4"])

    print("Sampled CPU time against the workload's own CLOCK_THREAD_CPUTIME_ID timers")
    for phase, function in (("serial", "sum_serial"), ("unrolled", "sum_unrolled4")):
        sampled_ms = self_cpu[function] / 1000
        note(f"{function}: sampled {sampled_ms:.0f} ms, timer {timings[phase].cpu_ms:.0f} ms")
        check(f"{function} self CPU is within 15% of the {phase} phase", within(sampled_ms, timings[phase].cpu_ms, 0.15))

    print("Inclusive cost and the call tree")
    for driver, kernel in (("drive_serial", "sum_serial"), ("drive_unrolled", "sum_unrolled4")):
        check(f"{driver} self CPU is under 5% of its inclusive CPU", self_cpu[driver] * 20 < incl_cpu[driver])
        check(f"{driver} inclusive CPU covers {kernel} self CPU", incl_cpu[driver] >= self_cpu[kernel] > 0)
    check("main inclusive CPU is at least 95% of the thread", incl_cpu["main"] * 100 >= 95 * total_cpu_us)

    print("One function, two callers")
    _, in_order_cpu = thread.cost_under("drive_chase_in_order", "chase")
    _, scattered_cpu = thread.cost_under("drive_chase_scattered", "chase")
    check("the flat profile has one chase row, equal to the sum over both callers",
          self_cpu["chase"] == in_order_cpu + scattered_cpu and in_order_cpu > 0 and scattered_cpu > 0)
    for phase, cpu_us in (("chase_in_order", in_order_cpu), ("chase_scattered", scattered_cpu)):
        note(f"chase under drive_{phase}: sampled {cpu_us / 1000:.0f} ms, timer {timings[phase].cpu_ms:.0f} ms")
        check(f"chase under drive_{phase} is within 20% of that phase", within(cpu_us / 1000, timings[phase].cpu_ms, 0.20))
    in_order_ns = timings["chase_in_order"].cpu_ms * 1e6 / IN_ORDER_HOPS
    scattered_ns = timings["chase_scattered"].cpu_ms * 1e6 / SCATTERED_HOPS
    note(f"CPU time per hop: in-order {in_order_ns:.2f} ns, scattered {scattered_ns:.1f} ns")
    check("a scattered hop costs at least 20 times an in-order hop", scattered_ns >= 20 * in_order_ns)

    print("Inlined code")
    frames = sidecar.debug_frames(binary, "chase")
    print("  symbol table name for the hot loop: chase")
    print(f"  debug-info frames at its first sampled address, innermost first: {' <- '.join(frames) if frames else 'none'}")
    check("hop, which has no symbol of its own, is named through debug info", bool(frames) and frames[0] == "hop")

    print("Off-CPU time")
    sleep = timings["sleep"]
    sleep_ticks, sleep_cpu = incl_ticks["sleep_phase"], incl_cpu["sleep_phase"]
    note(f"sleep_phase: {sleep_ticks} ticks, {sleep_cpu} us CPU; timer wall {sleep.wall_ms:.0f} ms, CPU {sleep.cpu_ms:.3f} ms")
    check("sleep_phase holds 40% to 105% of one tick per millisecond slept",
          0.40 * sleep.wall_ms <= sleep_ticks <= 1.05 * sleep.wall_ms)
    check("sleep_phase used under 5 ms of CPU", sleep_cpu < 5000)
    merged = max((weight for stack, weight, _ in thread.rows if "sleep_phase" in stack), default=0)
    note(f"largest single sample under sleep_phase weighs {merged} ticks")
    check("one sample with a weight above 1 carries at least 40% of those ticks", merged * 10 >= 4 * sleep_ticks > 0)
    check("its innermost frame is the kernel wait, not sleep_phase itself", self_ticks["sleep_phase"] == 0)

    speedup = timings["serial"].cpu_ms / timings["unrolled"].cpu_ms
    note(f"CPU time, one accumulator over four: {speedup:.2f}x")
    print("Measured speedup of four accumulators over one")
    check("at least 2x", speedup >= 2.0)
    return ok


def report_plain(profile: dict, sidecar: Sidecar, binary: str) -> bool:
    thread = main_thread(profile, sidecar)
    self_ticks, _ = thread.self_cost()
    incl_ticks, _ = thread.inclusive_cost()
    frames = sidecar.debug_frames(binary, "chase")
    callers = incl_ticks["drive_chase_in_order"] + incl_ticks["drive_chase_scattered"]
    no_inline_names = self_ticks["chase"] > 0 and frames is None
    no_callers = self_ticks["chase"] > 0 and callers == 0 and incl_ticks["main"] >= self_ticks["chase"]
    print("Same loop, built with -O2 alone (no -g, sibling calls allowed)")
    print(f"  debug-info frames for chase: {' <- '.join(frames) if frames else 'none'}")
    print(f"  chase has self samples and hop is not named anywhere: {verdict(no_inline_names)}")
    print(f"  drive_chase_in_order and drive_chase_scattered are in no stack (tail calls): {verdict(no_callers)}")
    return no_inline_names and no_callers


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile", type=Path)
    ap.add_argument("--binary", required=True, help="debug name of the profiled executable")
    ap.add_argument("--timings", type=Path, help="phase timings written by the workload")
    ap.add_argument("--plain", action="store_true", help="check the recording of the -O2-only build instead")
    args = ap.parse_args()
    try:
        profile, sidecar = load(args.profile)
        if args.plain:
            ok = report_plain(profile, sidecar, args.binary)
        else:
            if args.timings is None:
                ap.error("--timings is required without --plain")
            ok = report_full(profile, sidecar, read_timings(args.timings), args.binary)
    except (ProfileError, KeyError, IndexError, ValueError) as e:
        print(f"analyze_profile: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
