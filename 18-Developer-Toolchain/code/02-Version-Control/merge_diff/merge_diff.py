#!/usr/bin/env python3
"""merge_diff.py: Reference implementations and binary inspection utilities for Git diff, index stages, and rerere."""

import argparse
import os
import struct
import sys


def myers_diff(a_lines, b_lines):
    """Compute Shortest Edit Script (SES) and edit distance using Eugene Myers' 1986 greedy algorithm."""
    n = len(a_lines)
    m = len(b_lines)
    max_d = n + m
    v = {1: 0}
    trace = []

    for d in range(max_d + 1):
        v_copy = v.copy()
        trace.append(v_copy)
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
                x = v.get(k + 1, 0)
            else:
                x = v.get(k - 1, 0) + 1
            y = x - k
            while x < n and y < m and a_lines[x] == b_lines[y]:
                x += 1
                y += 1
            v[k] = x
            if x >= n and y >= m:
                return d, trace
    return max_d, trace


def backtrack_myers(a_lines, b_lines, trace):
    """Backtrack through Myers edit graph trace to reconstruct edit operations."""
    x = len(a_lines)
    y = len(b_lines)
    script = []

    for d in range(len(trace) - 1, 0, -1):
        v = trace[d]
        k = x - y
        if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v.get(prev_k, 0)
        prev_y = prev_x - prev_k

        while x > prev_x and y > prev_y:
            script.append(("KEEP", a_lines[x - 1]))
            x -= 1
            y -= 1

        if d > 0:
            if x == prev_x:
                script.append(("INS", b_lines[prev_y]))
            elif y == prev_y:
                script.append(("DEL", a_lines[prev_x]))
        x = prev_x
        y = prev_y

    while x > 0 and y > 0:
        script.append(("KEEP", a_lines[x - 1]))
        x -= 1
        y -= 1

    script.reverse()
    return script


def find_unique_common_anchors(a_lines, b_lines):
    """Find unique common lines that appear exactly once in sequence A and once in sequence B (Patience anchors)."""
    counts_a = {}
    pos_a = {}
    for idx, line in enumerate(a_lines):
        counts_a[line] = counts_a.get(line, 0) + 1
        pos_a[line] = idx

    counts_b = {}
    pos_b = {}
    for idx, line in enumerate(b_lines):
        counts_b[line] = counts_b.get(line, 0) + 1
        pos_b[line] = idx

    anchors = []
    for line, count in counts_a.items():
        if count == 1 and counts_b.get(line, 0) == 1:
            anchors.append((pos_a[line], pos_b[line], line.strip()))
    anchors.sort(key=lambda item: item[0])
    return anchors


def inspect_index(index_path):
    """Inspect Git binary index file and decode entry stages (bits 12-13 of flags)."""
    if not os.path.exists(index_path):
        print(f"Index file not found: {index_path}")
        return

    with open(index_path, "rb") as f:
        data = f.read()

    if len(data) < 12:
        print("Invalid index: file too small")
        return

    sig, ver, num_entries = struct.unpack(">4sII", data[:12])
    print(f"Index Header: Signature={sig.decode(errors='replace')}, Version={ver}, Entries={num_entries}")

    idx = 12
    for i in range(num_entries):
        entry_start = idx
        if idx + 62 > len(data):
            break
        fields = struct.unpack(">10I20sH", data[idx:idx + 62])
        mode = fields[6]
        oid = fields[10].hex()
        flags = fields[11]
        stage = (flags >> 12) & 0x3
        idx += 62

        path_bytes = bytearray()
        while idx < len(data) and data[idx] != 0:
            path_bytes.append(data[idx])
            idx += 1

        path = path_bytes.decode(errors="replace")
        entry_len = idx - entry_start + 1
        pad = (8 - (entry_len % 8)) if (entry_len % 8) != 0 else 0
        idx += 1 + pad

        stage_desc = {0: "normal", 1: "base", 2: "ours", 3: "theirs"}.get(stage, "unknown")
        print(f"  [{i}] stage={stage} ({stage_desc:<6}) mode={oct(mode)} oid={oid[:8]} path={path}")


def inspect_rr_cache(git_dir):
    """Inspect .git/rr-cache directory and summarize preimage/postimage conflict recordings."""
    cache_dir = os.path.join(git_dir, "rr-cache")
    if not os.path.isdir(cache_dir):
        print("No rr-cache directory found.")
        return

    entries = sorted(os.listdir(cache_dir))
    print(f"Found {len(entries)} recorded conflict signature(s) in rr-cache:")
    for sig in entries:
        sig_dir = os.path.join(cache_dir, sig)
        if not os.path.isdir(sig_dir):
            continue
        files = sorted(os.listdir(sig_dir))
        file_info = []
        for fn in files:
            fp = os.path.join(sig_dir, fn)
            sz = os.path.getsize(fp)
            file_info.append(f"{fn} ({sz} bytes)")
        print(f"  Conflict ID: {sig}")
        print(f"    Artifacts: {', '.join(file_info)}")


def main():
    parser = argparse.ArgumentParser(description="Git merge and diff internals inspector.")
    subparsers = parser.add_subparsers(dest="command")

    diff_parser = subparsers.add_parser("diff", help="Run Myers and Patience diff analysis on two files.")
    diff_parser.add_argument("file_a", help="First file")
    diff_parser.add_argument("file_b", help="Second file")

    idx_parser = subparsers.add_parser("index", help="Inspect binary .git/index stages.")
    idx_parser.add_argument("index_path", nargs="?", default=".git/index", help="Path to .git/index")

    rr_parser = subparsers.add_parser("rr-cache", help="Inspect .git/rr-cache conflict recordings.")
    rr_parser.add_argument("git_dir", nargs="?", default=".git", help="Path to .git directory")

    args = parser.parse_args()

    if args.command == "diff":
        with open(args.file_a, "r") as fa, open(args.file_b, "r") as fb:
            lines_a = fa.readlines()
            lines_b = fb.readlines()
        d, trace = myers_diff(lines_a, lines_b)
        script = backtrack_myers(lines_a, lines_b, trace)
        deletions = sum(1 for op, _ in script if op == "DEL")
        insertions = sum(1 for op, _ in script if op == "INS")
        print(f"Myers Edit Distance D={d} (Deletions={deletions}, Insertions={insertions})")

        anchors = find_unique_common_anchors(lines_a, lines_b)
        print(f"Patience Unique Common Line Anchors Found: {len(anchors)}")
        for pos_a, pos_b, text in anchors[:5]:
            print(f"  Line A:{pos_a + 1} == Line B:{pos_b + 1} -> '{text}'")

    elif args.command == "index":
        inspect_index(args.index_path)

    elif args.command == "rr-cache":
        inspect_rr_cache(args.git_dir)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
