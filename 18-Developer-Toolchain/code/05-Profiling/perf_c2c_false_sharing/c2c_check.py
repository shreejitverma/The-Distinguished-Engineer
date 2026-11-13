"""Turn perf c2c recordings of the packed and padded builds into derived facts.

Subcommands:
    pick-cpus          print two allowed CPUs that are on different physical cores
    report <workdir>   read the files run.sh left in <workdir> and print the facts

Sample counts differ from run to run, so nothing here prints a raw count: each
line states a threshold and whether the recording met it.

The memory-access decoding uses union perf_mem_data_src from
include/uapi/linux/perf_event.h: mem_op is bits 0-4, mem_snoop bits 19-23,
mem_snoopx bits 38-39.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import NoReturn

PERF_MEM_OP_LOAD = 0x02
PERF_MEM_OP_STORE = 0x04
PERF_MEM_OP_SHIFT = 0
PERF_MEM_SNOOP_HITM = 0x10
PERF_MEM_SNOOP_SHIFT = 19
PERF_MEM_SNOOPX_PEER = 0x02
PERF_MEM_SNOOPX_SHIFT = 38

MIN_SAMPLES = 100  # fewer contended samples than this is not a diagnosis
NOISE_SHARE = 0.05  # the padded build may keep this share of the packed count
TOTALS_SHARE = 0.20  # same idea for whole-process totals, which include thread start-up
READER_SHARE = 0.90  # share of contended loads that must be the reader's field
SPEEDUP = 1.2  # padded throughput over packed, both threads together

# perf script -F addr,data_src prints the address and the raw data_src in hex,
# then the decoded text that starts with "|OP".
SCRIPT_LINE = re.compile(r"^\s*([0-9a-f]+)\s+([0-9a-f]+)\s*\|")
TABLE_ROW = re.compile(r"^\s*(\d+)\s+(0x[0-9a-f]+)\s")
TOTAL_LINE = re.compile(
    r"^\s*(Load Local HITM|Load Remote HITM|Load HIT Local Peer|Load HIT Remote Peer)\s*:\s*(\d+)\s*$"
)


def fail(message: str) -> NoReturn:
    print(f"c2c_check: {message}", file=sys.stderr)
    sys.exit(1)


def yes(value: bool) -> str:
    return "yes" if value else "no"


def line_size() -> int:
    path = Path("/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size")
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError) as e:
        fail(f"cannot read {path}: {e}")


def core_of(cpu: int) -> tuple[int, int]:
    topo = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
    try:
        return int((topo / "physical_package_id").read_text()), int((topo / "core_id").read_text())
    except (OSError, ValueError) as e:
        fail(f"cannot read the topology of cpu{cpu}: {e}")


def pick_cpus() -> tuple[int, int]:
    """Two allowed CPUs on different physical cores, in one package when possible.

    Two hardware threads of one core share that core's L1 and L2 caches, so a
    line never has to travel between them and no HITM would be recorded.
    """
    cpus = sorted(os.sched_getaffinity(0))
    by_core: dict[tuple[int, int], int] = {}
    for cpu in cpus:
        by_core.setdefault(core_of(cpu), cpu)
    cores = sorted(by_core)
    if len(cores) < 2:
        fail(f"need two physical cores, the affinity mask covers {len(cores)}")
    for i, first in enumerate(cores):
        for second in cores[i + 1 :]:
            if first[0] == second[0]:
                return by_core[first], by_core[second]
    return by_core[cores[0]], by_core[cores[1]]


def read_info(path: Path) -> dict[str, int]:
    info: dict[str, int] = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        info[key] = int(value, 0)
    for key in ("base", "off_writer", "off_reader", "sizeof", "writer_iterations", "reader_iterations"):
        if key not in info:
            fail(f"{path} has no {key}")
    return info


class Samples:
    """Loads and stores from one recording, bucketed by data address."""

    def __init__(self, path: Path) -> None:
        self.loads_contended: dict[int, int] = {}
        self.stores: dict[int, int] = {}
        self.hitm = 0
        self.peer = 0
        self.parsed = 0
        unparsed = 0
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            m = SCRIPT_LINE.match(line)
            if not m:
                unparsed += 1
                continue
            self.parsed += 1
            addr = int(m.group(1), 16)
            src = int(m.group(2), 16)
            op = (src >> PERF_MEM_OP_SHIFT) & 0x1F
            hitm = (src >> PERF_MEM_SNOOP_SHIFT) & PERF_MEM_SNOOP_HITM
            peer = (src >> PERF_MEM_SNOOPX_SHIFT) & PERF_MEM_SNOOPX_PEER
            if op & PERF_MEM_OP_STORE:
                self.stores[addr] = self.stores.get(addr, 0) + 1
            if op & PERF_MEM_OP_LOAD and (hitm or peer):
                self.loads_contended[addr] = self.loads_contended.get(addr, 0) + 1
                self.hitm += 1 if hitm else 0
                self.peer += 1 if peer and not hitm else 0
        if self.parsed == 0:
            fail(f"{path}: no sample line matched the perf script -F addr,data_src format")
        if unparsed > self.parsed // 10:
            fail(f"{path}: {unparsed} of {unparsed + self.parsed} lines did not parse")

    def contended_on(self, lines: set[int], mask: int) -> int:
        return sum(n for addr, n in self.loads_contended.items() if addr & mask in lines)

    def contended_at(self, addr: int) -> int:
        return self.loads_contended.get(addr, 0)

    def stores_at(self, addr: int) -> int:
        return self.stores.get(addr, 0)


class C2CReport:
    """The two parts of `perf c2c report --stdio` that are stable to parse."""

    def __init__(self, path: Path) -> None:
        text = path.read_text()
        self.totals = 0
        seen = set()
        for line in text.splitlines():
            m = TOTAL_LINE.match(line)
            if m and m.group(1) not in seen:  # the first block is Trace Event Information
                seen.add(m.group(1))
                self.totals += int(m.group(2))
        if "Load Local HITM" not in seen:
            fail(f"{path}: no 'Load Local HITM' line; is this perf c2c report --stdio output?")
        start = text.find("Shared Data Cache Line Table")
        end = text.find("Shared Cache Line Distribution Pareto")
        if start < 0 or end < 0:
            fail(f"{path}: the cache line table headers are missing")
        self.rows = []
        for line in text[start:end].splitlines():
            m = TABLE_ROW.match(line)
            if m:
                self.rows.append((int(m.group(1)), int(m.group(2), 16)))

    def top_line(self) -> int | None:
        for index, addr in self.rows:
            if index == 0:
                return addr
        return None


def report(work: Path) -> None:
    size = line_size()
    mask = ~(size - 1)
    packed = read_info(work / "packed.info")
    padded = read_info(work / "padded.info")

    print(f"cache line size from sysfs coherency_line_size: {size} bytes")
    print()
    print("[layout]")
    for name, info in (("packed", packed), ("padded", padded)):
        if info["base"] & (size - 1):
            fail(f"{name}: the struct does not start on a cache line boundary")
        same = (info["off_writer"] & mask) == (info["off_reader"] & mask)
        print(
            f"{name}: writer_count at +{info['off_writer']}, reader_value at +{info['off_reader']}, "
            f"sizeof {info['sizeof']}, same cache line: {yes(same)}"
        )

    packed_line = {packed["base"] & mask}
    padded_lines = {(padded["base"] + padded["off_writer"]) & mask, (padded["base"] + padded["off_reader"]) & mask}

    print()
    print("[perf c2c record, decoded with perf script -F addr,data_src]")
    ps, ds = Samples(work / "packed.script"), Samples(work / "padded.script")
    on_line = ps.contended_on(packed_line, mask)
    at_reader = ps.contended_at(packed["base"] + packed["off_reader"])
    writer_stores = ps.stores_at(packed["base"] + packed["off_writer"])
    padded_contended = ds.contended_on(padded_lines, mask)
    kind = "HITM" if ps.hitm >= ps.peer else "peer"
    print(f"packed: contended load samples on the shared line, at least {MIN_SAMPLES}: {yes(on_line >= MIN_SAMPLES)}")
    print(
        f"packed: at least {READER_SHARE:.0%} of them load +{packed['off_reader']}, the reader's field: "
        f"{yes(on_line > 0 and at_reader >= READER_SHARE * on_line)}"
    )
    print(
        f"packed: store samples at +{packed['off_writer']}, the writer's field, at least {MIN_SAMPLES}: "
        f"{yes(writer_stores >= MIN_SAMPLES)}"
    )
    print(
        f"padded: contended load samples on the two fields' lines, at most {NOISE_SHARE:.0%} of packed: "
        f"{yes(padded_contended <= NOISE_SHARE * on_line)}"
    )
    print(f"snoop flag on the contended loads: {kind}")

    print()
    print("[perf c2c report --stdio]")
    pr, dr = C2CReport(work / "packed.c2c"), C2CReport(work / "padded.c2c")
    print(
        f"packed: HITM and peer loads in Trace Event Information, at least {MIN_SAMPLES}: "
        f"{yes(pr.totals >= MIN_SAMPLES)}"
    )
    print(f"packed: row 0 of the Shared Data Cache Line Table is the shared line: {yes(pr.top_line() in packed_line)}")
    print(
        f"padded: HITM and peer loads in Trace Event Information, at most {TOTALS_SHARE:.0%} of packed: "
        f"{yes(dr.totals <= TOTALS_SHARE * pr.totals)}"
    )

    print()
    print("[throughput without perf, same duration]")
    packed_free = read_info(work / "packed_free.info")
    padded_free = read_info(work / "padded_free.info")
    packed_total = packed_free["writer_iterations"] + packed_free["reader_iterations"]
    padded_total = padded_free["writer_iterations"] + padded_free["reader_iterations"]
    print(
        f"padded build completes at least {SPEEDUP:.1f}x the iterations of the packed build: "
        f"{yes(padded_total >= SPEEDUP * packed_total)}"
    )


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "pick-cpus":
        first, second = pick_cpus()
        print(first, second)
        return 0
    if len(argv) == 3 and argv[1] == "report":
        report(Path(argv[2]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
