"""Turn the raw output of the four benchmark stages into deterministic, falsifiable facts.

Usage: python3 check.py LOG_DIR CARGO_TARGET_DIR

Timings vary from run to run, so nothing here prints a time. Instead it re-derives
Criterion's estimates from its own saved samples, checks verdicts against wide margins,
and compares allocation and instruction counts with values predicted from first principles.
Any failed expectation prints "no" (or a wrong count) and makes the script exit non-zero.
"""

from __future__ import annotations

import json
import math
import platform
import re
import sys
from pathlib import Path

FAILURES: list[str] = []


def fact(label: str, ok: bool) -> None:
    print(f"{label}: {'yes' if ok else 'no'}")
    if not ok:
        FAILURES.append(label)


def close(a: float, b: float, rel: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=0.0)


def percentile(sorted_xs: list[float], p: float) -> float:
    """Criterion's Percentiles::at: linear interpolation at rank p/100 * (n - 1)."""
    n = len(sorted_xs) - 1
    if p == 100:
        return sorted_xs[n]
    rank = p / 100 * n
    lo = math.floor(rank)
    return sorted_xs[lo] + (sorted_xs[lo + 1] - sorted_xs[lo]) * (rank - lo)


def criterion(log: Path, target: Path) -> None:
    print("== Criterion.rs 0.8.2: sampling, estimates, change detection ==")
    bench = target / "criterion" / "checksum" / "n4096"
    sample = json.loads((bench / "new" / "sample.json").read_text())
    est = json.loads((bench / "new" / "estimates.json").read_text())
    tukey = json.loads((bench / "new" / "tukey.json").read_text())
    change = json.loads((bench / "change" / "estimates.json").read_text())

    iters = sample["iters"]
    times = sample["times"]
    print(f"sampling mode: {sample['sampling_mode']}")
    print(f"samples: {len(iters)}")
    d = iters[0]
    fact("iteration counts are d, 2d, ..., 100d", all(x == (i + 1) * d for i, x in enumerate(iters)))

    slope = sum(x * y for x, y in zip(iters, times)) / sum(x * x for x in iters)
    fact("slope point estimate equals sum(x*y) / sum(x*x) over sample.json", close(slope, est["slope"]["point_estimate"]))

    per_iter = [t / n for n, t in zip(iters, times)]
    mean = sum(per_iter) / len(per_iter)
    fact("mean point estimate equals mean(time / iters) over all 100 samples", close(mean, est["mean"]["point_estimate"]))

    s = sorted(per_iter)
    fact("median point estimate equals the interpolated 50th percentile", close(percentile(s, 50), est["median"]["point_estimate"]))

    q1, q3 = percentile(s, 25), percentile(s, 75)
    iqr = q3 - q1
    want = [q1 - 3 * iqr, q1 - 1.5 * iqr, q3 + 1.5 * iqr, q3 + 3 * iqr]
    fact("tukey.json fences equal Q1 - 3 IQR, Q1 - 1.5 IQR, Q3 + 1.5 IQR, Q3 + 3 IQR", all(close(a, b) for a, b in zip(want, tukey)))

    ci = est["slope"]["confidence_interval"]
    fact("slope 95% confidence interval contains the slope point estimate", ci["lower_bound"] <= est["slope"]["point_estimate"] <= ci["upper_bound"])

    out = (log / "crit_slow.out").read_text()
    verdicts = [v for v in ("Performance has regressed.", "Performance has improved.", "Change within noise threshold.", "No change in performance detected.") if v in out]
    print(f"slow build against baseline 'base': {verdicts[0] if len(verdicts) == 1 else 'unparsed'}")
    if verdicts != ["Performance has regressed."]:
        FAILURES.append("criterion verdict")
    m = re.search(r"\(p = ([0-9.]+) ([<>]) ([0-9.]+)\)", out)
    fact("p-value printed below the 0.05 significance level", bool(m) and m.group(2) == "<" and m.group(3) == "0.05")
    mc = change["mean"]
    fact("lower bound of the mean-change interval is above the +1% noise threshold", mc["confidence_interval"]["lower_bound"] > 0.01)
    fact("mean change point estimate is between +200% and +500%", 2.0 < mc["point_estimate"] < 5.0)

    import hashlib

    now = hashlib.sha256((bench / "base" / "sample.json").read_bytes()).hexdigest()
    before = (log / "base_hash_before").read_text().strip()
    fact("--baseline base left base/sample.json byte-for-byte unchanged", now == before)


LABEL = re.compile(r"\b(max alloc|alloc|dealloc|grow|shrink):")


def divan_counts(text: str, bench: str) -> dict[str, int]:
    """Per-iteration operation counts for one bench from Divan's tree output.

    Divan prints a label row ("alloc:") and, under it, a count row whose first column
    is the fastest sample's count per iteration and a size row in bytes.
    """
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.search(rf"\b{bench}\b", ln))
    counts: dict[str, int] = {}
    i = start + 1
    while i < len(lines) and not re.search(r"\bpush_\w+\b", lines[i]):
        m = LABEL.search(lines[i])
        if m and i + 1 < len(lines):
            cells = [c.strip() for c in re.split("\u2502", lines[i + 1])]
            first = next((c for c in cells if c), "")
            tok = first.split()[-1] if first else ""
            if tok.replace(".", "", 1).isdigit():
                counts[m.group(1)] = int(float(tok))
        i += 1
    return counts


def divan(log: Path) -> None:
    print("== Divan 0.1.21: AllocProfiler counts per iteration, n = 1000 ==")
    text = (log / "divan.out").read_text()
    grow = divan_counts(text, "push_grow")
    resv = divan_counts(text, "push_reserved")
    for name, c in (("push_grow", grow), ("push_reserved", resv)):
        print(f"{name}: alloc {c.get('alloc', 0)}, grow {c.get('grow', 0)}, shrink {c.get('shrink', 0)}, dealloc {c.get('dealloc', 0)}")
    fact("push_grow: 1 alloc and 8 grows (capacity 4 doubled to 1024)", grow.get("alloc") == 1 and grow.get("grow") == 8)
    fact("push_reserved: 1 alloc and 0 grows", resv.get("alloc") == 1 and resv.get("grow", 0) == 0)


def predicted_growth(n: int, elem: int) -> tuple[int, int, int]:
    """RawVec::grow_amortized for size_of::<T>() == elem: (blocks, total bytes, final bytes)."""
    min_cap = 8 if elem == 1 else (4 if elem <= 1024 else 1)
    cap, blocks, total = 0, 0, 0
    for length in range(n):
        if length == cap:
            cap = max(cap * 2, length + 1, min_cap)
            blocks += 1
            total += cap * elem
    return blocks, total, cap * elem


def parse_stats(path: Path) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", path.read_text())}


def dhat(log: Path) -> None:
    print("== dhat 0.3.3: heap usage testing, n = 1000 ==")
    blocks, total, final = predicted_growth(1000, 8)
    g = parse_stats(log / "dhat_grow.out")
    r = parse_stats(log / "dhat_reserved.out")
    print(f"predicted from RawVec growth: {blocks} blocks, {total} bytes, final buffer {final} bytes")
    for name, s in (("push_grow", g), ("push_reserved", r)):
        print(f"{name}: total_blocks {s['total_blocks']}, total_bytes {s['total_bytes']}, max_blocks {s['max_blocks']}, max_bytes {s['max_bytes']}, curr_bytes {s['curr_bytes']}")
    fact("push_grow matches the prediction (each realloc counts as a block)", (g["total_blocks"], g["total_bytes"], g["max_bytes"]) == (blocks, total, final))
    fact("push_reserved is one 8000-byte block", (r["total_blocks"], r["total_bytes"], r["max_bytes"]) == (1, 8000, 8000))
    fact("everything was freed before HeapStats::get (curr_bytes 0)", g["curr_bytes"] == 0 and r["curr_bytes"] == 0)
    code = (log / "dhat_assert.code").read_text().strip()
    saved = (log / "dhat_assert.json").read_text().strip() == "yes"
    err = (log / "dhat_assert.err").read_text()
    fact("failing dhat::assert_eq! panics (exit 101) and saves dhat-heap.json", code == "101" and saved and "dhat: assertion failed" in err)


HEADER = re.compile(r"^\S.*::(push_grow|push_reserved|checksum)\b")
IR = re.compile(r"^\s+Instructions:\s+(\d+)\|(\S+)\s+\(([^)]*)\)")


def gungraun_ir(text: str) -> dict[str, tuple[int, str, str]]:
    out: dict[str, tuple[int, str, str]] = {}
    current = ""
    for ln in text.splitlines():
        h = HEADER.match(ln)
        if h:
            current = h.group(1)
            continue
        m = IR.match(ln)
        if m and current:
            out[current] = (int(m.group(1)), m.group(2), m.group(3))
    return out


def gungraun(log: Path) -> None:
    print("== Gungraun 0.20.0: Callgrind instruction counts ==")
    if platform.system() != "Linux":
        print(f"skipped on {platform.system().lower()}: Valgrind has no port for this system (the stage runs on Linux)")
        return
    first = gungraun_ir((log / "gung_first.out").read_text())
    second = gungraun_ir((log / "gung_second.out").read_text())
    slow = gungraun_ir((log / "gung_slow.out").read_text())
    names = ["push_grow", "push_reserved", "checksum"]
    fact("first run reports all 3 benchmarks", all(n in first for n in names))
    fact("second identical run: Instructions '(No change)' for 3 of 3", all(second.get(n, (0, "", ""))[2] == "No change" for n in names))
    fact("push_grow executes more instructions than push_reserved", first["push_grow"][0] > first["push_reserved"][0])
    ratio = slow["checksum"][0] / first["checksum"][0]
    fact("slow checksum executes between 3.5x and 4.5x the instructions", 3.5 < ratio < 4.5)
    fact("push_* instruction counts unchanged by the slow feature", all(slow[n][2] == "No change" for n in ("push_grow", "push_reserved")))
    err = (log / "gung_slow.err").read_text()
    out = (log / "gung_slow.out").read_text()
    fact("--callgrind-limits='ir=5%' fails the run with exit status 3", "exit status: 3" in err and "Regressions:" in out)


def main() -> int:
    log, target = Path(sys.argv[1]), Path(sys.argv[2])
    criterion(log, target)
    divan(log)
    dhat(log)
    gungraun(log)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} expectation(s)", file=sys.stderr)
        for f in FAILURES:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
