#!/usr/bin/env python3
"""Summarize a macOS sample(1) report for the service's worker threads.

Prints, for threads whose stack contains score_batch, how many samples sit
under score_batch -> PyGILState_Ensure and how many of those are inside
take_gil.  Usage: sample_stacks.py <report.txt>
"""

from __future__ import annotations

import re
import sys

# "    + ! : 683 PyGILState_Ensure  (in Python) + 76  [0x...]"
NODE = re.compile(
    r"^(?P<lead>[ +!:|]*?)(?P<count>\d+) (?P<name>\S+)  \(in (?P<image>[^)]+)\)"
)
THREAD = re.compile(r"^(?P<lead> +)(?P<count>\d+) Thread_")


def summarize(text: str) -> dict[str, int]:
    """Return sample counts for worker threads: total, under Ensure, under take_gil."""
    lines = text.splitlines()
    try:
        start = (
            next(i for i, line in enumerate(lines) if line.startswith("Call graph:"))
            + 1
        )
    except StopIteration:
        raise SystemExit("sample_stacks.py: no 'Call graph:' section in the report")
    totals = {"worker_samples": 0, "under_ensure": 0, "under_take_gil": 0}
    thread_total = 0
    thread = {"is_worker": False, "under_ensure": 0, "under_take_gil": 0}
    stack: list[tuple[int, str]] = []  # (column of the count, function name)

    def flush() -> None:
        if thread["is_worker"]:
            totals["worker_samples"] += thread_total
            totals["under_ensure"] += thread["under_ensure"]
            totals["under_take_gil"] += thread["under_take_gil"]

    for line in lines[start:]:
        if not line.strip() or line.startswith("Total number in stack"):
            break
        t = THREAD.match(line)
        if t:
            flush()
            thread_total = int(t.group("count"))
            thread = {"is_worker": False, "under_ensure": 0, "under_take_gil": 0}
            stack = []
            continue
        m = NODE.match(line)
        if not m:
            continue
        col, name, count = len(m.group("lead")), m.group("name"), int(m.group("count"))
        while stack and stack[-1][0] >= col:
            stack.pop()
        ancestors = [n for _, n in stack]
        if name == "score_batch":
            thread["is_worker"] = True
        if name == "PyGILState_Ensure" and "score_batch" in ancestors:
            thread["under_ensure"] += count
        if (
            name == "take_gil"
            and "PyGILState_Ensure" in ancestors
            and "score_batch" in ancestors
        ):
            thread["under_take_gil"] += count
        stack.append((col, name))
    flush()
    return totals


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        result = summarize(fh.read())
    print(" ".join(f"{k}={v}" for k, v in result.items()))
