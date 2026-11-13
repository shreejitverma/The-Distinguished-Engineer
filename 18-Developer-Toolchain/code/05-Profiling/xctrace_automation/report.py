"""Turn two xctrace recordings of workload.c into facts that do not change between runs.

Sample counts, timestamps, pids, and core numbers vary from run to run, so nothing here prints
them. Every line is either a value read from the tools (a template name, a PMI threshold) or a
yes/no answer to a question with a band wide enough for sampling noise and narrow enough that a
broken mechanism answers "no". Any "no" also exits non-zero.
"""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from trace_xml import Export, Table, TraceXMLError, integer, text

HOT, WARM = "hot_leaf", "warm_leaf"
KPC_CLASSES = ("FIXED", "CONFIGURABLE", "POWER", "RAWPMU")  # bit positions 0..3, osfmk/kern/kpc.h
FAILED: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"{label}: {'yes' if ok else 'no'}")
    if not ok:
        FAILED.append(label)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class Sample:
    time_ns: int
    weight: int
    frames: tuple[str, ...]  # leaf first
    stack_was_ref: bool

    @property
    def leaf(self) -> str:
        return self.frames[0] if self.frames else "?"

    @property
    def folded(self) -> str:
        return ";".join(reversed(self.frames))


def only_table(export: Export, schema: str) -> Table:
    found = [t for t in export.tables if t.schema == schema]
    if len(found) != 1:
        raise TraceXMLError(f"expected one {schema} table, found {len(found)}")
    return found[0]


def samples(export: Export, table: Table) -> list[Sample]:
    return [
        Sample(
            time_ns=integer(row["time"]),
            weight=integer(row["weight"]),
            frames=export.frames(row["stack"]),
            stack_was_ref="stack" in refs,
        )
        for row, refs in zip(table.rows, table.ref_cells, strict=True)
    ]


def share(rows: list[Sample], leaf: str) -> float:
    total = sum(s.weight for s in rows)
    return 100.0 * sum(s.weight for s in rows if s.leaf == leaf) / total


def heaviest_stacks(rows: list[Sample], count: int) -> list[str]:
    weight: Counter[str] = Counter()
    for s in rows:
        weight[s.folded] += s.weight
    return [stack for stack, _ in weight.most_common(count)]


def toc_tables(toc: ET.Element, schema: str) -> list[ET.Element]:
    return [t for t in toc.iter("table") if t.get("schema") == schema]


def function_body(disassembly: str, symbol: str) -> list[str]:
    """Instruction lines of one function in `otool -tV` output."""
    body: list[str] = []
    inside = False
    for line in disassembly.splitlines():
        if line.endswith(":") and not line.startswith(("0", "\t", " ")):
            inside = line == f"_{symbol}:"
            continue
        if inside:
            body.append(" ".join(line.split()[1:]))
    if not body:
        raise TraceXMLError(f"otool printed no body for _{symbol}")
    return body


def section_binary(work: Path) -> None:
    print("== 1. The binary under test (otool -tV)")
    dis = run(["otool", "-tV", str(work / "workload")]).stdout
    hot, middle = function_body(dis, HOT), function_body(dis, "middle")
    saves_frame = any(i.startswith("stp x29, x30") for i in hot)
    print(f"{HOT} saves a frame record (stp x29, x30): {'yes' if saves_frame else 'no'}")
    print(f"middle calls {HOT} with: {next(i for i in middle if HOT in i)}")
    print(f"middle reaches {WARM} with: {next(i for i in middle if WARM in i)}")


def section_time_profiler(work: Path) -> tuple[list[Sample], int]:
    print("\n== 2. Time Profiler: xctrace record, then export --toc")
    toc = ET.parse(work / "tp-toc.xml").getroot()
    print(f"template-name: {toc.findtext('run/info/summary/template-name')}")
    process = toc.find("run/info/target/process")
    assert process is not None
    print(
        f"target: type={process.get('type')} name={process.get('name')} "
        f"return-exit-status={process.get('return-exit-status')}"
    )
    check("toc lists exactly one time-profile table", len(toc_tables(toc, "time-profile")) == 1)
    raw = toc_tables(toc, "time-sample")
    check("toc lists exactly one time-sample table", len(raw) == 1)
    print(
        f"time-sample: sample-rate-micro-seconds={raw[0].get('sample-rate-micro-seconds')} "
        f"callstack={raw[0].get('callstack')} all-thread-states={raw[0].get('all-thread-states')}"
    )
    profile = toc_tables(toc, "time-profile")[0]
    print(
        f"time-profile: high-frequency-sampling={profile.get('high-frequency-sampling')} "
        f"record-waiting-threads={profile.get('record-waiting-threads')} "
        f"needs-kernel-callstack={profile.get('needs-kernel-callstack')}"
    )

    print("\n== 3. Time Profiler: export --xpath, id/ref resolved")
    export = Export(work / "tp.xml")
    table = only_table(export, "time-profile")
    print("columns: " + " ".join(c.mnemonic for c in table.columns))
    weight_col = table.column("weight")
    print(f"weight column: name={weight_col.name} engineering-type={weight_col.engineering_type}")
    rows = samples(export, table)
    check("at least 500 samples", len(rows) >= 500)
    weights = {s.weight for s in rows}
    print("distinct sample weights (ns): " + " ".join(str(w) for w in sorted(weights)))
    states = {text(row["thread-state"]) for row in table.rows}
    print("thread states sampled: " + " ".join(sorted(states)))
    by_ref = sum(1 for s in rows if s.stack_was_ref)
    check("more than 90% of rows carry their backtrace as a ref", by_ref > 0.9 * len(rows))
    inline = [s for s in rows if not s.stack_was_ref]
    check(
        "a reader that skips ref rows keeps under 10% of the weight",
        sum(s.weight for s in inline) < 0.1 * sum(s.weight for s in rows),
    )
    # A sample taken while the thread has no user stack to walk (the last one, during exit) has
    # an empty cell in the stack column.
    check("under 1% of rows have an empty backtrace cell", sum(1 for s in rows if not s.frames) < 0.01 * len(rows))

    print("\n== 4. Time Profiler: where the time went (designed 3:1)")
    hot, warm = share(rows, HOT), share(rows, WARM)
    check(f"{HOT} self weight in [70, 80] %", 70.0 <= hot <= 80.0)
    check(f"{WARM} self weight in [20, 30] %", 20.0 <= warm <= 30.0)
    check("the two leaves hold more than 98% of the weight", hot + warm > 98.0)
    print("heaviest two folded stacks:")
    for stack in heaviest_stacks(rows, 2):
        print(f"  {stack}")

    shown = (work / "tp-show.txt").read_text()
    line = next((ln for ln in shown.splitlines() if ln.rstrip().endswith(HOT)), None)
    if line is None:
        raise TraceXMLError(f"xctrace show printed no row for {HOT}")
    percent = re.search(r"([\d.]+)%", line.split("\t")[1])
    if percent is None:
        raise TraceXMLError(f"no percentage in xctrace show row: {line!r}")
    check(f"xctrace show --calltree agrees on {HOT} within 0.5 points", abs(float(percent.group(1)) - hot) <= 0.5)
    return rows, sum(s.weight for s in rows)


def section_signpost(work: Path, rows: list[Sample]) -> None:
    print("\n== 5. Signpost interval from the same trace")
    export = Export(work / "tp-signpost.xml")
    tables = [t for t in export.tables if t.schema == "os-signpost"]
    events: dict[tuple[int, str], str] = {}
    for table in tables:
        for row in table.rows:
            if text(row["subsystem"]) == "lab.xctrace" and text(row["name"]) == "compute":
                events[(integer(row["time"]), text(row["event-type"]))] = row["message"].get("fmt", "")
    print(f"tables matched by the XPath: {len(tables)}")
    print("distinct compute events: " + " ".join(kind for _, kind in sorted(events)))
    begins = [(t, msg) for (t, kind), msg in events.items() if kind == "Begin"]
    ends = [t for (t, kind) in events if kind == "End"]
    check("exactly one Begin and one End", len(begins) == 1 and len(ends) == 1)
    (begin, message), end = begins[0], ends[0]
    print(f"Begin message: {message}")
    inside = [s for s in rows if begin <= s.time_ns <= end]
    check("at least 98% of samples fall inside the interval", len(inside) >= 0.98 * len(rows))
    check(
        "on-CPU weight inside the interval does not exceed its wall time (2% slack)",
        sum(s.weight for s in inside) <= 1.02 * (end - begin),
    )


def section_cpu_profiler(work: Path) -> str:
    print("\n== 6. CPU Profiler: the same workload sampled by a PMU counter")
    toc = ET.parse(work / "cp-toc.xml").getroot()
    print(f"template-name: {toc.findtext('run/info/summary/template-name')}")
    pmi = toc_tables(toc, "kdebug-counters-with-pmi-sample")
    check("toc lists exactly one kdebug-counters-with-pmi-sample table", len(pmi) == 1)
    event = (pmi[0].get("pmi-event") or "").strip('"')
    threshold = int(pmi[0].get("pmi-threshold") or 0)
    print(f"pmi-event={event} pmi-threshold={threshold} callstack={pmi[0].get('callstack')}")

    export = Export(work / "cp.xml")
    table = only_table(export, "cpu-profile")
    weight_col = table.column("weight")
    print(f"weight column: name={weight_col.name} engineering-type={weight_col.engineering_type}")
    rows = samples(export, table)
    check("at least 500 samples", len(rows) >= 500)
    check("sample weights are not all equal", len({s.weight for s in rows}) > 1)
    heaviest = max(s.weight for s in rows)
    check("the largest weight is the PMI threshold plus under 5% skid", threshold <= heaviest <= 1.05 * threshold)
    check(f"{HOT} share of cycles in [70, 80] %", 70.0 <= share(rows, HOT) <= 80.0)
    return event


def time_sample_attrs(toc_path: Path) -> str:
    toc = ET.parse(toc_path).getroot()
    raw = toc_tables(toc, "time-sample")
    if len(raw) != 1:
        raise TraceXMLError(f"{toc_path.name}: expected one time-sample table, found {len(raw)}")
    return (
        f"sample-rate-micro-seconds={raw[0].get('sample-rate-micro-seconds')} "
        f"all-thread-states={raw[0].get('all-thread-states')}"
    )


def section_options(work: Path) -> None:
    print("\n== 7. Recording options (--show-recording-options, --recording-options)")
    options = json.loads((work / "options.json").read_text())
    print("instruments with options: " + ", ".join(sorted(options)))
    keys = " ".join(f"{k}={json.dumps(v)}" for k, v in sorted(options["Time Profiler"].items()))
    print(f"Time Profiler defaults: {keys}")
    check(
        "a file holding only the changed key is rejected", (work / "partial.status").read_text().strip() == "rejected"
    )

    print(f"highFrequencySampling=true -> {time_sample_attrs(work / 'hf-toc.xml')}")
    export = Export(work / "hf.xml")
    rows = samples(export, only_table(export, "time-profile"))
    print("  distinct sample weights (ns): " + " ".join(str(w) for w in sorted({s.weight for s in rows})))
    check(f"  {HOT} self weight still in [70, 80] %", 70.0 <= share(rows, HOT) <= 80.0)
    print(f"recordWaitingThreads=true -> {time_sample_attrs(work / 'wt-toc.xml')}")


def sysctl(name: str) -> int:
    proc = run(["sysctl", "-n", name])
    if proc.returncode != 0:
        raise TraceXMLError(f"sysctl {name} failed: {proc.stderr.strip()}")
    return int(proc.stdout)


def section_kpep(pmi_event: str) -> None:
    print("\n== 8. The PMU as the kernel describes it")
    name = "cpu_{:x}_{:x}_{:x}.plist".format(sysctl("hw.cputype"), sysctl("hw.cpusubtype"), sysctl("hw.cpufamily"))
    path = Path("/usr/share/kpep") / name
    check("/usr/share/kpep has a database named after hw.cputype, hw.cpusubtype, hw.cpufamily", path.is_file())
    with path.open("rb") as fh:
        cpu = plistlib.load(fh)["system"]["cpu"]
    fixed, config, power = cpu["fixed_counters"], cpu["config_counters"], cpu["power_counters"]
    print(f"fixed_counters mask: {fixed:#x} ({fixed.bit_count()} counters)")
    print(f"config_counters mask: {config:#x} ({config.bit_count()} counters)")
    print(f"power_counters mask: {power:#x} ({power.bit_count()} counters)")
    check("the fixed and configurable masks do not overlap", fixed & config == 0)
    check("the power mask is a subset of the configurable mask", power & config == power)
    events = cpu["events"]
    fixed_events = sorted((e["fixed_counter"], n) for n, e in events.items() if "fixed_counter" in e)
    print("fixed events: " + " ".join(f"{n}=counter{i}" for i, n in fixed_events))
    print("aliases: " + " ".join(f"{a}->{t}" for a, t in sorted(cpu["aliases"].items())))
    check(f"the CPU Profiler's PMI event {pmi_event} is in the database", pmi_event in events)
    any_counter = [n for n, e in events.items() if "counters_mask" not in e]
    check(f"{pmi_event} may be programmed on any configurable counter", pmi_event in any_counter)
    restricted = [n for n, e in events.items() if "fixed_counter" not in e and "counters_mask" in e]
    check("more than 10 events are restricted to a subset of the counters", len(restricted) > 10)
    check(
        "every restricted event's mask is a subset of the configurable mask",
        all(events[n]["counters_mask"] & config == events[n]["counters_mask"] for n in restricted),
    )

    classes = sysctl("kpc.classes")
    bits = " ".join(f"{n}={'yes' if classes >> i & 1 else 'no'}" for i, n in enumerate(KPC_CLASSES))
    print(f"kpc.classes: {classes} ({classes:#b}) -> {bits}")
    for name in ("kperf.limits.timer_min_period_ns", "kperf.limits.timer_min_pet_period_ns"):
        print(f"{name}: {sysctl(name)}")


def section_dtrace() -> None:
    print("\n== 9. DTrace as an unprivileged user")
    uid = int(run(["id", "-u"]).stdout)
    check("running as a non-root user", uid != 0)
    print(run(["csrutil", "status"]).stdout.strip())
    proc = run(["dtrace", "-l"])
    print(f"dtrace -l: exit {proc.returncode}, {len(proc.stdout.splitlines())} probes listed")
    for line in proc.stderr.strip().splitlines():
        print(f"  {line}")


def main(argv: list[str]) -> int:
    work = Path(argv[1])
    section_binary(work)
    rows, _ = section_time_profiler(work)
    section_signpost(work, rows)
    pmi_event = section_cpu_profiler(work)
    section_options(work)
    section_kpep(pmi_event)
    section_dtrace()
    if FAILED:
        print("\n".join(f"check failed: {label}" for label in FAILED), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
