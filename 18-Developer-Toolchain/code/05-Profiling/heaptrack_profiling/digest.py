"""Reduce heaptrack_print text, Massif output files, and DHAT JSON to stable facts.

Every number printed here is either exact (a count or a byte total from a
deterministic -O0 workload) or a difference between two runs of the same
binary, so start-up allocations made by the C++ runtime cancel out.
Any parse failure raises, so a format change fails the lab instead of
printing a wrong number.

Usage:
    digest.py heaptrack N NONE.txt CHURN.txt REUSE.txt PMR.txt PEAKLEAK.txt
    digest.py massif PEAK.out SMALL_HALF.out SMALL_FULL.out N_HALF N_FULL
    digest.py dhat DHAT.json
"""

from __future__ import annotations

import json
import re
import sys

UNITS = {"B": 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
MIB = 1024 * 1024


def fail(msg: str) -> None:
    raise SystemExit(f"digest: {msg}")


# ---------------------------------------------------------------- heaptrack


def summary_int(text: str, label: str) -> int:
    m = re.search(rf"^{re.escape(label)}: (\d+) \(", text, re.MULTILINE)
    if m is None:
        fail(f"heaptrack_print summary has no '{label}' line")
    return int(m.group(1))


def heaptrack_bytes(token: str) -> float:
    """heaptrack_print formats bytes in powers of 1000 with one-letter units."""
    m = re.fullmatch(r"(-?[\d.]+)([BKMGT])", token)
    if m is None:
        fail(f"unexpected heaptrack byte format: {token!r}")
    return float(m.group(1)) * UNITS[m.group(2)]


def leak_entry(text: str, function: str) -> tuple[str, int]:
    lines = text.splitlines()
    try:
        start = lines.index("MEMORY LEAKS")
    except ValueError:
        fail("heaptrack_print output has no MEMORY LEAKS section (was --print-leaks=true passed?)")
    # printMerged() writes, per allocation site: a header ending in "from" (no
    # colon), the site itself (operator new frames are already stripped by the
    # analyzer), up to --sub-peak-limit caller backtraces under "... from:"
    # headers, and a blank line. The section ends at the next section header.
    groups: list[tuple[re.Match[str], list[str]]] = []
    for line in lines[start + 1 :]:
        if line == "MOST TEMPORARY ALLOCATIONS" or line.startswith("total runtime:"):
            break
        m = re.fullmatch(r"(\S+) leaked over (\d+) calls from", line)
        if m:
            groups.append((m, []))
        elif groups:
            groups[-1][1].append(line)
    if not groups:
        fail("MEMORY LEAKS section has no entries")
    # Prefer the group whose site line names the function; fall back to any
    # group whose caller backtraces name it, so a missed frame strip still matches.
    for match_site in (True, False):
        for m, body in groups:
            hit = function in body[0] if (match_site and body) else any(function in b for b in body)
            if hit:
                return m.group(1), int(m.group(2))
    fail(f"no leak entry attributed to {function}")
    raise AssertionError


def heaptrack(n: str, none: str, churn: str, reuse: str, pmr: str, peakleak: str) -> None:
    texts = {}
    for name, path in (("none", none), ("churn", churn), ("reuse", reuse), ("pmr", pmr), ("peakleak", peakleak)):
        with open(path, encoding="utf-8", errors="replace") as f:
            texts[name] = f.read()
    base_calls = summary_int(texts["none"], "calls to allocation functions")
    base_temp = summary_int(texts["none"], "temporary memory allocations")
    print(f"heaptrack, deltas against an empty run of the same binary ({n} iterations each):")
    for name in ("churn", "reuse", "pmr"):
        calls = summary_int(texts[name], "calls to allocation functions") - base_calls
        temp = summary_int(texts[name], "temporary memory allocations") - base_temp
        print(f"  {name:<6} allocation calls {calls:+d}, temporary allocations {temp:+d}")

    m = re.search(r"^peak heap memory consumption: (\S+)$", texts["peakleak"], re.MULTILINE)
    if m is None:
        fail("heaptrack_print summary has no peak heap line")
    peak = heaptrack_bytes(m.group(1))
    print(f"  peakleak peak heap consumption is between 64 and 65 MiB: {'yes' if 64 * MIB <= peak < 65 * MIB else 'no'}")
    shown, calls = leak_entry(texts["peakleak"], "leak_on_purpose")
    print(f"  peakleak leak entry for leak_on_purpose: {shown} leaked over {calls} calls")


# ------------------------------------------------------------------ Massif


def massif_snapshots(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    snaps = []
    for chunk in re.split(r"^snapshot=\d+\n", text, flags=re.MULTILINE)[1:]:
        fields = dict(re.findall(r"^(mem_heap_B|mem_heap_extra_B|mem_stacks_B|heap_tree)=(\S+)$", chunk, re.MULTILINE))
        fields["body"] = chunk
        snaps.append(fields)
    if not snaps:
        fail(f"{path}: no snapshots")
    return snaps


def massif_peak(path: str) -> dict[str, str]:
    peaks = [s for s in massif_snapshots(path) if s.get("heap_tree") == "peak"]
    if len(peaks) != 1:
        fail(f"{path}: expected exactly one peak snapshot, found {len(peaks)}")
    return peaks[0]


def massif(peak_out: str, half_out: str, full_out: str, n_half: str, n_full: str) -> None:
    peak = massif_peak(peak_out)
    site = [int(b) for b in re.findall(r"^\s*n\d+: (\d+) .*transient_peak", peak["body"], re.MULTILINE)]
    if not site:
        fail("peak snapshot tree has no node for transient_peak")
    print("Massif:")
    print(f"  peak snapshot: total useful heap {int(peak['mem_heap_B']) // MIB} MiB (rounded down), "
          f"of which transient_peak holds {max(site)} bytes")

    half, full = massif_peak(half_out), massif_peak(full_out)
    blocks = int(n_full) - int(n_half)
    useful = (int(full["mem_heap_B"]) - int(half["mem_heap_B"])) / blocks
    extra = (int(full["mem_heap_extra_B"]) - int(half["mem_heap_extra_B"])) / blocks
    print(f"  malloc(1) blocks, difference of two peaks ({n_half} vs {n_full} live blocks): "
          f"useful-heap {useful:g} bytes per block, extra-heap {extra:g} bytes per block")


# -------------------------------------------------------------------- DHAT


def decode_acc(acc: list[int]) -> dict[int, int]:
    """DHAT run-length encoding: a negative n means 'the next value, n times'."""
    counts: dict[int, int] = {}
    i = 0
    while i < len(acc):
        if acc[i] < 0:
            reps, value = -acc[i], acc[i + 1]
            i += 2
        else:
            reps, value = 1, acc[i]
            i += 1
        counts[value] = counts.get(value, 0) + reps
    return counts


def dhat_pps(data: dict, function: str) -> list[dict]:
    ftbl = data["ftbl"]
    return [pp for pp in data["pps"] if any(function in ftbl[f] for f in pp["fs"])]


def dhat(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("mode") != "heap" or not data.get("bklt") or not data.get("bkacc"):
        fail("DHAT file is not a heap-mode profile with lifetimes and accesses")
    threshold = data["tuth"]
    print(f"DHAT (short-lived threshold tuth = {threshold} {data['tu']}):")

    churn = dhat_pps(data, "parse_record(")
    if len(churn) != 1:
        fail(f"expected one program point under parse_record, found {len(churn)}")
    pp = churn[0]
    avg_life = pp["tl"] // pp["tbk"]
    print(f"  parse_record: {pp['tbk']} blocks of {pp['tb'] // pp['tbk']} bytes, "
          f"max live {pp['mbk']}, live at t-end {pp['ebk']}")
    print(f"    average lifetime {avg_life} {data['tu']}, short-lived: {'yes' if avg_life < threshold else 'no'}")
    print(f"    reads {pp['rb'] // pp['tbk']} bytes and writes {pp['wb'] // pp['tbk']} bytes per block")
    if "acc" not in pp:
        fail("parse_record program point has no access histogram")
    hist = ", ".join(f"{n} bytes accessed {v} times" for v, n in sorted(decode_acc(pp["acc"]).items()))
    print(f"    per-byte access histogram: {hist}")

    wo = dhat_pps(data, "write_only_buffer(")
    if len(wo) != 1:
        fail(f"expected one program point under write_only_buffer, found {len(wo)}")
    pp = wo[0]
    print(f"  write_only_buffer: {pp['tbk']} blocks, {pp['tb']} bytes, reads {pp['rb']} bytes, "
          f"writes {pp['wb']} bytes, access histogram present: {'yes' if 'acc' in pp else 'no'}")


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        fail(__doc__ or "usage")
    cmd, args = argv[1], argv[2:]
    if cmd == "heaptrack" and len(args) == 6:
        heaptrack(*args)
    elif cmd == "massif" and len(args) == 5:
        massif(*args)
    elif cmd == "dhat" and len(args) == 1:
        dhat(*args)
    else:
        fail(f"bad arguments: {argv[1:]}")


if __name__ == "__main__":
    main(sys.argv)
