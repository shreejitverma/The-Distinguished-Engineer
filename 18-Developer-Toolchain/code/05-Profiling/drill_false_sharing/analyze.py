#!/usr/bin/env python3
"""Checks for the false-sharing drill: layout, throughput, and what `perf c2c` reports.

stdout carries only pass facts that are the same on every run; the measured numbers go to stderr.
Every check exits non-zero with a reason when the mechanism it tests is absent.

Stages (FS_DRILL_STAGES, comma separated, default all four):
  selftest    parse the recorded `perf c2c report --stdio` fixtures and classify them
  layout      run each variant and reason about the addresses it used
  throughput  compare the slowest worker of each buggy variant with its fix, best of five runs each
  c2c         record each buggy variant with `perf c2c record` and read the report
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

VARIANTS = ("array", "array-fixed", "heap", "heap-fixed", "total", "total-fixed")
FIXES = (("array", "array-fixed"), ("heap", "heap-fixed"), ("total", "total-fixed"))
SPEEDUP = 1.3  # a fix must make the slowest worker at least this much faster; no effect reads 1.0
MIN_CONTENTION = 100  # HITM or peer-snoop load samples a contended variant must show
REPEATS = (
    5  # timing rounds; each variant's best round is compared, which damps host noise
)
QUIET_SHARE = (
    0.10  # a fixed variant must show under this fraction of the buggy variant's samples
)
HOT_OFFSET = (
    0.05  # an offset is hot when it carries this share of the line's contention
)
OWNER = 0.90  # one thread "owns" an offset when it carries this share of the offset's contention
SHARER = 0.10  # a thread "shares" the top offset when it carries this share of it

TRACE_TITLE = "Trace Event Information"
GLOBAL_TITLE = "Global Shared Cache Line Event Information"
PARETO_TITLE = "Shared Cache Line Distribution Pareto"
CONTENTION_LABELS = (
    "Load Local HITM",
    "Load Remote HITM",
    "Load HIT Local Peer",
    "Load HIT Remote Peer",
)

COUNT_RE = re.compile(r"^\s+(\S.*?\S)\s+:\s+(\d+)\s*$")
LINE_HEAD_RE = re.compile(r"^\s+(\d+)(?:\s+\d+)+\s+(0x[0-9a-f]+)\s*$")
PERCENT_RE = re.compile(r"^\d+\.\d+%$")
THREAD_RE = re.compile(r"^(\d+):(\S+)$")


def fail(msg: str) -> None:
    print(f"drill_false_sharing: FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def note(msg: str) -> None:
    print(f"drill_false_sharing: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# perf c2c report --stdio -c tid,iaddr
# ---------------------------------------------------------------------------------------------


@dataclass
class Row:
    """One offset row of the Pareto table: who touched which byte of the line, and how painfully."""

    share: float  # remote plus local HITM percentage (peer-snoop percentage on Arm SPE)
    offset: int
    thread: str
    symbol: str


@dataclass
class Line:
    address: int
    rows: list[Row] = field(default_factory=list)


@dataclass
class Report:
    counts: dict[str, int]
    lines: list[Line]

    @property
    def contention(self) -> int:
        return sum(self.counts.get(label, 0) for label in CONTENTION_LABELS)


def parse_row(tokens: list[str]) -> Row | None:
    shares = []
    for tok in tokens:
        if not PERCENT_RE.match(tok):
            break
        shares.append(float(tok[:-1]))
    if len(shares) < 2:
        return None
    rest = tokens[len(shares) :]
    if not rest or not rest[0].startswith("0x"):
        return None
    # Keep "tid:comm" whole: the tid tells threads apart even when two share a comm.
    thread = next((m.group(0) for m in map(THREAD_RE.match, rest) if m), "")
    symbol = ""
    for i, tok in enumerate(rest[:-1]):
        if tok in ("[.]", "[k]"):
            symbol = rest[i + 1]
            break
    # The first two columns are the costly-snoop shares: remote and local HITM, or remote and local peer.
    return Row(
        share=shares[0] + shares[1],
        offset=int(rest[0], 16),
        thread=thread,
        symbol=symbol,
    )


def parse_report(text: str) -> Report:
    """Read the totals table and the per-line offset table; raise ValueError on an unknown layout."""
    start = text.find(TRACE_TITLE)
    end = text.find(GLOBAL_TITLE)
    pareto = text.find(PARETO_TITLE)
    if start < 0 or end < start or pareto < end:
        raise ValueError(
            "not a `perf c2c report --stdio` output: section titles are missing"
        )
    counts = {}
    for raw in text[start:end].splitlines():
        m = COUNT_RE.match(raw)
        if m:
            counts[m.group(1)] = int(m.group(2))
    if "Load Local HITM" not in counts or "Load Operations" not in counts:
        raise ValueError("the Trace Event Information table has no HITM totals")
    lines: list[Line] = []
    for raw in text[pareto + len(PARETO_TITLE) :].splitlines():
        if raw.startswith("#") or not raw.strip() or set(raw.strip()) <= {"-", "="}:
            continue
        head = LINE_HEAD_RE.match(raw)
        if head:
            lines.append(Line(address=int(head.group(2), 16)))
            continue
        row = parse_row(raw.split())
        if row is None or not lines:
            raise ValueError(f"unreadable Pareto row: {raw.strip()[:80]}")
        lines[-1].rows.append(row)
    return Report(counts=counts, lines=lines)


def classify(line: Line) -> str:
    """Name the access pattern on one line: 'false', 'true', or the reason it is neither."""
    by_offset: dict[int, dict[str, float]] = {}
    for row in line.rows:
        if row.share > 0:
            threads = by_offset.setdefault(row.offset, {})
            threads[row.thread] = threads.get(row.thread, 0.0) + row.share
    total = sum(sum(t.values()) for t in by_offset.values())
    if total <= 0:
        return "no contention on the line"
    hot = {
        off: t for off, t in by_offset.items() if sum(t.values()) >= HOT_OFFSET * total
    }
    top = max(hot, key=lambda off: sum(hot[off].values()))
    top_total = sum(hot[top].values())
    if sum(1 for v in hot[top].values() if v >= SHARER * top_total) >= 2:
        return "true"  # several threads fight over the same bytes
    owners = set()
    for off, threads in hot.items():
        owner, weight = max(threads.items(), key=lambda kv: kv[1])
        if weight < OWNER * sum(threads.values()):
            return f"offset {off:#x} has no single owner"
        owners.add(owner)
    if len(owners) < 2:
        return "only one thread is hot on the line"
    return "false"  # different threads, different bytes, one line


def hot_symbols(line: Line) -> set[str]:
    return {row.symbol for row in line.rows if row.share > 0}


def selftest(fixtures: Path) -> None:
    """The parser must tell the recorded reports apart; a parser that always says 'false' fails here."""
    want = {
        "array": ("false", 328, 0x555555574140),
        "array-peer": ("false", 328, 0x555555574140),
        "heap": ("false", 328, 0x55555A0002C0),
        "total": ("true", 264, 0x555555574100),
    }
    for name, (pattern, contention, address) in want.items():
        report = parse_report((fixtures / f"{name}.c2c.txt").read_text())
        if report.contention != contention:
            fail(
                f"selftest {name}: contention {report.contention}, expected {contention}"
            )
        if not report.lines or report.lines[0].address != address:
            fail(f"selftest {name}: hottest line is not {address:#x}")
        got = classify(report.lines[0])
        if got != pattern:
            fail(f"selftest {name}: classified as {got!r}, expected {pattern!r}")
        if hot_symbols(report.lines[0]) != {"ingest"}:
            fail(
                f"selftest {name}: hot symbols are {sorted(hot_symbols(report.lines[0]))}"
            )
    quiet = parse_report((fixtures / "array-fixed.c2c.txt").read_text())
    if quiet.contention != 0 or quiet.lines:
        fail("selftest array-fixed: a report with no HITM must have no contended lines")
    try:
        parse_report("perf: not a report\n")
    except ValueError:
        pass
    else:
        fail("selftest: garbage input was accepted as a report")
    print(
        "selftest: parser tells false sharing, true sharing, and a quiet run apart on 5 recorded reports: ok"
    )


# ---------------------------------------------------------------------------------------------
# Running the workload
# ---------------------------------------------------------------------------------------------


def read_int(path: Path, default: int) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return default


def pick_cpus() -> list[int]:
    """Up to four allowed CPUs on distinct physical cores of one package.

    Two hyperthreads of one core share an L1 cache, so a line never moves between them and the
    drill would show nothing.
    """
    packages: dict[int, dict[int, int]] = {}
    for cpu in sorted(os.sched_getaffinity(0)):
        topo = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        package = read_int(topo / "physical_package_id", 0)
        core = read_int(topo / "core_id", cpu)
        packages.setdefault(package, {}).setdefault(core, cpu)
    cores = max(packages.values(), key=len)
    cpus = sorted(cores.values())[:4]
    if len(cpus) < 2:
        fail("need at least 2 physical cores in the allowed CPU set")
    return cpus


def run_child(cmd: list[str], what: str, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        fail(f"{what} did not finish within {timeout}s")
    except OSError as e:
        fail(f"{what} could not start: {e}")
    raise AssertionError("unreachable")


def facts_or_fail(out: str, what: str) -> dict[str, str]:
    try:
        return parse_facts(out)
    except ValueError as e:
        fail(f"{what}: {e}")
    raise AssertionError("unreachable")


def parse_facts(out: str) -> dict[str, str]:
    for raw in out.splitlines():
        if raw.startswith("variant="):
            return dict(tok.split("=", 1) for tok in raw.split())
    raise ValueError("the workload printed no facts line")


def run_ingest(
    binary: Path, variant: str, cpus: list[int], secs: float
) -> dict[str, str]:
    cmd = [str(binary), variant, ",".join(map(str, cpus)), f"{secs}"]
    proc = run_child(cmd, f"ingest {variant}", 120)
    if proc.returncode != 0:
        fail(f"ingest {variant} exited {proc.returncode}: {proc.stderr.strip()}")
    return facts_or_fail(proc.stdout, f"ingest {variant}")


def addresses(facts: dict[str, str], key: str) -> list[int]:
    return [int(a) for a in facts[key].split(",")]


def sharing_workers(facts: dict[str, str], line: int) -> int:
    """How many workers have a hot word on a cache line that another worker also uses."""
    quota, msgs = addresses(facts, "quota_addrs"), addresses(facts, "msgs_addrs")
    lines = [{q // line, m // line} for q, m in zip(quota, msgs)]
    return sum(
        1
        for i, mine in enumerate(lines)
        if any(mine & other for j, other in enumerate(lines) if j != i)
    )


def rate(facts: dict[str, str]) -> float:
    return int(facts["slowest"]) / float(facts["secs"])


def layout(runs: dict[str, dict[str, str]], line: int, announce: bool) -> None:
    """Check one round of runs; print the facts only when announce is set, so they appear once."""
    say = print if announce else (lambda *_: None)
    workers = int(runs["array"]["workers"])
    for variant, facts in runs.items():
        if facts["handled"] != facts["counted"]:
            fail(
                f"{variant}: handled {facts['handled']} messages but counted {facts['counted']}"
            )
    say("layout: every variant counted exactly the messages it handled: yes")

    def expect(variant: str, ok: bool, text: str) -> None:
        if not ok:
            fail(
                f"layout {variant}: {sharing_workers(runs[variant], line)} of {workers} workers share a line; "
                f"expected {text} (line size {line})"
            )
        say(
            f"layout: {variant:<12} workers that share a cache line with another worker: {text}"
        )

    expect("array", sharing_workers(runs["array"], line) == workers, "all")
    expect("array-fixed", sharing_workers(runs["array-fixed"], line) == 0, "none")
    expect("heap", sharing_workers(runs["heap"], line) >= 2, "at least two")
    expect("heap-fixed", sharing_workers(runs["heap-fixed"], line) == 0, "none")
    for variant in ("total", "total-fixed"):
        facts = runs[variant]
        counter = set(addresses(facts, "msgs_addrs"))
        quota_lines = {q // line for q in addresses(facts, "quota_addrs")}
        if len(counter) != 1 or counter.pop() // line in quota_lines:
            fail(
                f"layout {variant}: the shared counter does not have a cache line to itself"
            )
    say("layout: total        the one shared counter has a cache line to itself: yes")


def throughput(rounds: list[dict[str, dict[str, str]]]) -> None:
    """Judge each fix by its best run against the bug's best run.

    The best run is the one the host disturbed least. Taking it for the bug as well can only
    understate the gain, so noise can fail this check but cannot pass it.
    """
    for bug, fix in FIXES:
        best_bug = max(rate(r[bug]) for r in rounds)
        best_fix = max(rate(r[fix]) for r in rounds)
        gain = best_fix / max(best_bug, 1.0)
        note(
            f"{bug:<6} {best_bug / 1e6:8.1f} M msgs/s  {fix:<12} {best_fix / 1e6:8.1f} M msgs/s"
            f"  ratio {gain:5.2f} (slowest worker, best of {len(rounds)} runs)"
        )
        if gain < SPEEDUP:
            fail(
                f"throughput: {fix} is only {gain:.2f}x {bug} (best of {len(rounds)} runs each); "
                f"expected at least {SPEEDUP}x"
            )
        print(
            f"throughput: slowest worker of {fix:<12} is at least {SPEEDUP}x that of {bug}: yes"
        )


# ---------------------------------------------------------------------------------------------
# perf c2c
# ---------------------------------------------------------------------------------------------


def perf_line_size() -> int:
    """The line size `perf c2c report` groups by: tools/perf/util/cacheline.c."""
    try:
        size = os.sysconf("SC_LEVEL1_DCACHE_LINESIZE")
    except (ValueError, OSError):
        size = 0
    if size <= 0:
        size = read_int(
            Path("/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size"), 0
        )
    if size <= 0:
        fail("cannot tell which cache line size perf groups by")
    return size


def c2c_run(
    perf: str, binary: Path, variant: str, cpus: list[int], secs: float, work: Path
) -> tuple[dict[str, str], Report]:
    data = work / f"{variant}.perf.data"
    record = [
        perf,
        "c2c",
        "record",
        "--all-user",
        "--",
        "-o",
        str(data),
        str(binary),
        variant,
        ",".join(map(str, cpus)),
        f"{secs}",
    ]
    rec = run_child(record, f"perf c2c record of {variant}", 300)
    if rec.returncode != 0 and "ingest: " in rec.stderr:
        # perf record passes the workload's exit status through; do not blame the PMU for it.
        fail(
            f"the workload failed under perf c2c record ({variant}):\n{rec.stderr.strip()[-1500:]}"
        )
    if rec.returncode != 0:
        fail(
            f"`{' '.join(record[:5])} ...` exited {rec.returncode}. perf c2c needs memory sampling "
            f"(Intel PEBS, AMD IBS on Zen 4 or later, Arm SPE) and, because it adds --phys-data, "
            f"kernel.perf_event_paranoid <= 1 or CAP_PERFMON.\n{rec.stderr.strip()[-1500:]}"
        )
    facts = facts_or_fail(rec.stdout, f"perf c2c record of {variant}")
    report_cmd = [perf, "c2c", "report", "-i", str(data), "--stdio", "-c", "tid,iaddr"]
    rep = run_child(report_cmd, f"perf c2c report of {variant}", 300)
    (work / f"{variant}.c2c.txt").write_text(rep.stdout)
    if rep.returncode != 0:
        fail(
            f"perf c2c report on {variant} exited {rep.returncode}:\n{rep.stderr.strip()[-1500:]}"
        )
    try:
        return facts, parse_report(rep.stdout)
    except ValueError as e:
        fail(f"perf c2c report on {variant}: {e}")
        raise


def c2c(binary: Path, cpus: list[int], secs: float, work: Path) -> None:
    perf = shutil.which("perf")
    if perf is None:
        fail("perf is not on PATH")
    size = perf_line_size()
    got = {
        v: c2c_run(perf, binary, v, cpus, secs, work)
        for v in ("array", "array-fixed", "heap", "total")
    }
    for variant, (_, report) in got.items():
        note(
            f"{variant:<12} contention samples {report.contention:7d} of {report.counts['Load Operations']:7d} loads"
        )

    def hottest(variant: str) -> tuple[dict[str, str], Line]:
        facts, report = got[variant]
        if report.contention < MIN_CONTENTION:
            fail(
                f"c2c {variant}: {report.contention} HITM or peer-snoop load samples; expected at least {MIN_CONTENTION}"
            )
        if not report.lines:
            fail(f"c2c {variant}: the report lists no shared cache line")
        return facts, report.lines[0]

    def shard_lines(facts: dict[str, str]) -> set[int]:
        return {
            a // size * size
            for key in ("quota_addrs", "msgs_addrs")
            for a in addresses(facts, key)
        }

    def pattern(variant: str, line: Line, want: str, text: str) -> None:
        seen = classify(line)
        if seen != want:
            fail(
                f"c2c {variant}: line {line.address:#x} looks like {seen!r}, expected {want!r} sharing"
            )
        print(f"c2c: {variant:<12} pattern on the hottest line: {text}")

    facts, line = hottest("array")
    if line.address not in shard_lines(facts):
        fail(f"c2c array: hottest line {line.address:#x} holds no worker shard")
    print(
        f"c2c: array        contended load samples (HITM or peer snoop) at least {MIN_CONTENTION}: yes"
    )
    print("c2c: array        hottest line holds the packed shards: yes")
    pattern(
        "array", line, "false", "false sharing (each hot offset belongs to one thread)"
    )
    symbols = hot_symbols(line)

    quiet = got["array-fixed"][1].contention
    loud = got["array"][1].contention
    if quiet >= QUIET_SHARE * loud:
        fail(
            f"c2c array-fixed: {quiet} contended samples against {loud} for array; expected under {QUIET_SHARE:.0%}"
        )
    print(
        f"c2c: array-fixed  contended load samples under {QUIET_SHARE:.0%} of array: yes"
    )

    facts, line = hottest("heap")
    if line.address not in shard_lines(facts):
        fail(f"c2c heap: hottest line {line.address:#x} holds no worker shard")
    if not int(facts["heap_lo"]) <= line.address < int(facts["heap_hi"]):
        fail(f"c2c heap: hottest line {line.address:#x} is outside the [heap] mapping")
    print("c2c: heap         hottest line holds malloc'd shards inside [heap]: yes")
    pattern(
        "heap", line, "false", "false sharing (each hot offset belongs to one thread)"
    )
    symbols |= hot_symbols(line)

    facts, line = hottest("total")
    if line.address != addresses(facts, "msgs_addrs")[0] // size * size:
        fail(f"c2c total: hottest line {line.address:#x} is not the shared counter")
    print("c2c: total        hottest line holds the shared counter: yes")
    pattern("total", line, "true", "true sharing (several threads on one offset)")
    symbols |= hot_symbols(line)

    if symbols != {"ingest"}:
        fail(f"c2c: hot accesses come from {sorted(symbols)}, expected only ingest")
    print("c2c: all three bugs are hot in the same function, ingest: yes")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--binary", type=Path, required=True)
    ap.add_argument("--fixtures", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument(
        "--line",
        type=int,
        required=True,
        help="the cache line size ingest was built for",
    )
    args = ap.parse_args()

    known = ("selftest", "layout", "throughput", "c2c")
    stages = [
        s for s in os.environ.get("FS_DRILL_STAGES", ",".join(known)).split(",") if s
    ]
    unknown = [s for s in stages if s not in known]
    if unknown:
        fail(f"unknown stage {unknown[0]!r}; known: {', '.join(known)}")
    try:
        secs = float(os.environ.get("FS_DRILL_SECS", "1.0"))
    except ValueError:
        fail("FS_DRILL_SECS must be a number of seconds")
    # The c2c stage records for 2 * secs, and ingest accepts at most 60 seconds.
    if not 0.0 < secs <= 30.0:
        fail("FS_DRILL_SECS must be in (0, 30]")

    if "selftest" in stages:
        selftest(args.fixtures)
    if not {"layout", "throughput", "c2c"} & set(stages):
        return 0
    cpus = pick_cpus()
    note(
        f"workers pinned to cpus {cpus}, cache line {args.line} bytes, {secs}s per run"
    )
    if {"layout", "throughput"} & set(stages):
        # Every round runs all six variants, so a slow spell on the host cannot hit one variant only.
        rounds = [
            {v: run_ingest(args.binary, v, cpus, secs) for v in VARIANTS}
            for _ in range(REPEATS)
        ]
        if "layout" in stages:
            # ASLR moves the addresses between runs, so every round is checked.
            for i, runs in enumerate(rounds):
                layout(runs, args.line, announce=i == 0)
        if "throughput" in stages:
            throughput(rounds)
    if "c2c" in stages:
        c2c(args.binary, cpus, 2 * secs, args.work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
