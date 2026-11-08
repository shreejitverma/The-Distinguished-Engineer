"""Run the TSan and MSan lab stages and print only facts that do not vary between runs.

Usage: python3 driver.py <build_dir>

Reports are reduced to access kinds, sizes, source lines, report counts, and exit
statuses, so addresses, PIDs, thread IDs, and frame decorations never reach stdout.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ACCESS_RE = re.compile(
    r"^  ((?:Previous )?(?:atomic )?(?:Read|Write|read|write)) of size (\d+) at \S+ by "
    r"(main thread|thread T\d+)"
)
FRAME0_RE = re.compile(r"^\s+#0 .*?([A-Za-z_]+\.cpp):(\d+)")
LOCATION_RE = re.compile(r"^  Location is global '([^']+)'")
SUMMARY_RE = re.compile(r"^SUMMARY: ThreadSanitizer: ([a-z -]+?) \S*?([A-Za-z_]+\.cpp):(\d+)")
USER_FRAME_RE = re.compile(r"^\s+#\d+ 0x[0-9a-fA-F]+ in ([A-Za-z_][A-Za-z0-9_:]*)\S* .*?([A-Za-z_]+\.cpp):(\d+)")
ASM_FUNC_RE = re.compile(r"^_?([a-z_]+):")
ASM_CALL_RE = re.compile(r"\b_?(__tsan_[A-Za-z0-9_]+)")
TSAN_SCENARIOS = (
    "racy_counter",
    "mutex_counter",
    "relaxed_publish",
    "acqrel_publish",
    "fence_publish",
    "sleep_ordered",
)
QUIET = "atexit_sleep_ms=0:abort_on_error=0"


def run(cmd: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env, timeout=120)


def instrumentation(asm: Path) -> None:
    print("== Stage 1: calls the TSan pass inserted (instr.cpp, -O1, from the assembly) ==")
    calls: dict[str, list[str]] = {}
    current = None
    for line in asm.read_text().splitlines():
        m = ASM_FUNC_RE.match(line)
        if m and not line.startswith((".", "L", "l_")):
            current = m.group(1)
            calls.setdefault(current, [])
            continue
        if current is None:
            continue
        c = ASM_CALL_RE.search(line)
        if c and c.group(1) != "__tsan_init":
            calls[current].append(c.group(1))
    for fn in ("plain_write", "plain_increment", "const_read", "escaped_local",
               "release_store", "acquire_load", "release_fence"):
        seq = calls.get(fn)
        if seq is None:
            raise SystemExit(f"function {fn} not found in {asm}")
        print(f"  {fn:16} {' '.join(seq) if seq else '(no runtime calls)'}")
    print()


def tsan_report(stderr: str) -> list[str]:
    out: list[str] = []
    threads: set[str] = set()
    lines = stderr.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("WARNING: ThreadSanitizer: "):
            kind = line[len("WARNING: ThreadSanitizer: "):].split(" (pid=")[0]
            out.append(f"    report: {kind}")
        m = ACCESS_RE.match(line)
        if m:
            loc = "?"
            if i + 1 < len(lines) and (f := FRAME0_RE.match(lines[i + 1])):
                loc = f"{f.group(1)}:{f.group(2)}"
            out.append(f"    {m.group(1)} of size {m.group(2)} at {loc}")
            threads.add("main" if m.group(3) == "main thread" else "T" + m.group(3).split("T")[-1])
        m = LOCATION_RE.match(line)
        if m:
            out.append(f"    location: global '{m.group(1)}'")
        if line.startswith("  As if synchronized via sleep:"):
            out.append("    note: as if synchronized via sleep")
        m = SUMMARY_RE.match(line)
        if m:
            out.append(f"    summary: {m.group(1)} at {m.group(2)}:{m.group(3)}")
    if threads:
        out.append(f"    threads involved: {', '.join(sorted(threads))}")
    return out


def tsan_stage(build: Path) -> None:
    print(f"== Stage 2: race reports (races.cpp, TSAN_OPTIONS={QUIET}) ==")
    for name in TSAN_SCENARIOS:
        proc = run([str(build / "races"), name], {"TSAN_OPTIONS": QUIET})
        warnings = sum(1 for ln in proc.stderr.splitlines() if ln.startswith("WARNING: ThreadSanitizer: "))
        print(f"  {name}: warnings={warnings} exit={proc.returncode}")
        for ln in tsan_report(proc.stderr):
            print(ln)
    print()
    print("== Stage 3: exit status of a racy run under different TSAN_OPTIONS ==")
    for opts in ("atexit_sleep_ms=0", QUIET, "atexit_sleep_ms=0:abort_on_error=0:exitcode=3",
                 "atexit_sleep_ms=0:report_bugs=0"):
        proc = run([str(build / "races"), "racy_counter"], {"TSAN_OPTIONS": opts})
        status = f"signal {-proc.returncode}" if proc.returncode < 0 else f"exit {proc.returncode}"
        warnings = sum(1 for ln in proc.stderr.splitlines() if ln.startswith("WARNING: ThreadSanitizer: "))
        print(f"  TSAN_OPTIONS={opts}: {status}, warnings={warnings}")
    print()


def msan_report(stderr: str) -> list[str]:
    out: list[str] = []
    lines = stderr.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("==") and "WARNING: MemorySanitizer: " in line:
            out.append(f"    report: {line.split('WARNING: MemorySanitizer: ')[1]}")
            for nxt in lines[i + 1:]:
                if f := USER_FRAME_RE.match(nxt):
                    out.append(f"    first user frame: {f.group(1)} at {f.group(2)}:{f.group(3)}")
                    break
        elif line.startswith("  Uninitialized value was "):
            for nxt in lines[i + 1:]:
                if f := USER_FRAME_RE.match(nxt):
                    out.append(f"    origin: {line.strip().rstrip(':')} in {f.group(1)} at {f.group(2)}:{f.group(3)}")
                    break
            else:
                out.append(f"    origin: {line.strip().rstrip(':')}")
    return out


def msan_stage(build: Path, accepted: bool, why: str) -> None:
    print("== Stage 4: MemorySanitizer ==")
    if not accepted:
        print(f"  -fsanitize=memory rejected by this compiler: {why}")
        print("  MSan stage skipped; the MSan report shape is described in the note.")
        return
    print("  -fsanitize=memory accepted")
    for exe in ("msan_eager", "msan_lazy"):
        proc = run([str(build / exe)], {"MSAN_OPTIONS": "abort_on_error=0"})
        print(f"  {exe}: exit={proc.returncode} stdout={proc.stdout.strip()!r}")
        for ln in msan_report(proc.stderr):
            print(ln)


def main() -> int:
    build = Path(sys.argv[1])
    instrumentation(build / "instr.s")
    tsan_stage(build)
    probe = (build / "msan_probe.txt").read_text().strip()
    accepted = probe == "accepted"
    msan_stage(build, accepted, probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
