#!/usr/bin/env python3
"""Read per-unit durations out of the HTML report that `cargo build --timings` writes.

The report embeds its data as `const UNIT_DATA = [...]`, one object per compiler invocation
with `name`, `target`, `duration` in seconds, and, for library units, `sections` splitting
the time into frontend and codegen. Durations vary from run to run, so this script prints
only comparisons with a very wide margin plus report structure, and writes the raw seconds and
noisy ratios to stderr.

Usage: timings.py cargo-timing.html
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def units(report: Path) -> list[dict]:
    m = re.search(
        r"const UNIT_DATA = (\[.*?\]);\s*\nconst ", report.read_text(), re.DOTALL
    )
    if not m:
        raise SystemExit(f"no UNIT_DATA in {report}: the report format changed")
    return json.loads(m.group(1))


def label(unit: dict) -> str:
    target = unit["target"].strip().replace('"', "")
    return f"{unit['name']} {target}".strip()


def main(argv: list[str]) -> int:
    data = units(Path(argv[0]))
    by_label = {label(u): u["duration"] for u in data}
    for name, seconds in sorted(by_label.items(), key=lambda kv: -kv[1]):
        print(f"unrecorded: {seconds:6.2f}s  {name}", file=sys.stderr)

    slowest = max(by_label, key=by_label.get)
    print(f"units compiled: {len(data)}")
    print(f"slowest unit: {slowest}")
    bloated = by_label["apps app_bloated bin"]
    for other in ("app_lean", "app_dyn"):
        # Wall time of a parallel cold build: the margin does not hold on every run, so the
        # ratio goes to stderr with the raw seconds and never into the checked output.
        ratio = bloated / max(by_label[f"apps {other} bin"], 1e-9)
        print(f"unrecorded: app_bloated / {other} = {ratio:.1f}x", file=sys.stderr)
    # Structure of the report, which is deterministic: which units carry a frontend/codegen split.
    split = {"lib": [0, 0], "bin": [0, 0]}
    for u in data:
        kind = "bin" if "bin" in u["target"] else "lib"
        split[kind][1] += 1
        if u.get("sections"):
            split[kind][0] += 1
    for kind in ("lib", "bin"):
        have, total = split[kind]
        print(f"{kind} units with a frontend/codegen section split: {have} of {total}")
    kit = by_label["kit_bloated"]
    print(
        f"kit_bloated, the crate that defines the generic code, took under a tenth of app_bloated: "
        f"{'yes' if kit * 10 < bloated else 'no'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
