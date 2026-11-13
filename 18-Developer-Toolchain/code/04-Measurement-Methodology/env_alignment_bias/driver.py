"""Drive the probe binaries and print only facts that are stable from run to run.

Usage: python3 driver.py BUILD_DIR

BUILD_DIR holds probe (default build) and four link-order variants, as built by run.sh.
Every child runs with an explicit environment, so the parent shell cannot leak in.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LAUNCHES = 64
PAD_SIZES = (0, 1, 15, 16, 63, 64, 4095, 4096)


def run(build: Path, exe: str, mode: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        [f"./{exe}", mode],
        cwd=build,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"{exe} {mode} failed with exit {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


def yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def part_strings(build: Path) -> None:
    print(
        "== A. Environment bytes move the exec string area by exactly that many bytes =="
    )
    print(
        f"{'PAD chars':>9}  {'argv[0] page offset':>19}  {'moved mod 4096':>14}  layout prediction"
    )
    base = None
    all_match = True
    for n in PAD_SIZES:
        actual, predicted = (
            int(x) for x in run(build, "probe", "strings", {"PAD": "x" * n}).split()
        )
        if base is None:
            base = actual
        moved = (base - actual) % 4096
        match = actual == predicted
        all_match &= match
        print(
            f"{n:>9}  {actual:>19}  {moved:>14}  {'matches' if match else 'MISMATCH ' + str(predicted)}"
        )
    print(
        f"argv[0] offset equals the page-aligned-top prediction for every size: {yes_no(all_match)}"
    )
    print()


def part_frame(build: Path) -> None:
    print(f"== B. The same binary and environment, launched {LAUNCHES} times ==")
    offsets = [int(run(build, "probe", "frame", {"PAD": ""})) for _ in range(LAUNCHES)]
    slots = sorted({(o % 64) // 16 for o in offsets})
    one_line = sum(1 for o in offsets if o % 64 == 0)
    print(
        f"buffer address mod 16 identical in every launch: {yes_no(len({o % 16 for o in offsets}) == 1)}"
    )
    print(f"distinct 16-byte slots used within a 64-byte line: {len(slots)} of 4")
    print(f"distinct page offsets seen: more than one: {yes_no(len(set(offsets)) > 1)}")
    print(
        f"64-byte buffer fits in one cache line in some launches: {yes_no(one_line > 0)}"
    )
    print(
        f"64-byte buffer straddles two cache lines in some launches: {yes_no(one_line < LAUNCHES)}"
    )
    print()


def part_shift(build: Path) -> None:
    print("== C. A harness can move the frame itself: alloca(k) in the caller ==")
    print(f"{'k bytes':>7}  {'frame moved by':>14}  {'cache-line slot change':>22}")
    for line in run(build, "probe", "shift", {"PAD": ""}).splitlines():
        k, moved = (int(x) for x in line.split())
        print(f"{k:>7}  {moved:>14}  {(moved % 64) // 16:>22}")
    print()


def part_code(build: Path) -> None:
    print("== D. Link order moves an unchanged function ==")
    results = {}
    for align in ("default", "align64"):
        for order in ("pad_first", "hot_first"):
            exe = f"probe_{align}_{order}"
            line_offset, hot_minus_pad = (
                int(x) for x in run(build, exe, "code", {"PAD": ""}).split()
            )
            results[(align, order)] = line_offset
            print(
                f"{align:>8} alignment, {order:<9}: hot_loop offset within its 64-byte line = "
                f"{line_offset:>2}, hot_loop - lab_pad = {hot_minus_pad:>4} bytes"
            )
    d = results[("default", "pad_first")] != results[("default", "hot_first")]
    a = results[("align64", "pad_first")] == results[("align64", "hot_first")] == 0
    print(f"default build: line offset depends on link order: {yes_no(d)}")
    print(f"-falign-functions=64 build: offset 0 in both orders: {yes_no(a)}")


def main() -> None:
    build = Path(sys.argv[1])
    part_strings(build)
    part_frame(build)
    part_shift(build)
    part_code(build)


if __name__ == "__main__":
    main()
