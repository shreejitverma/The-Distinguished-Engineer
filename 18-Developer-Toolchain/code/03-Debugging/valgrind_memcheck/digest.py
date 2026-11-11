"""Reduce a Valgrind log to the facts that do not change between runs.

Kept: each error's heading and detail lines, stack frames that point into the
lab's own sources (function and file:line), the innermost frame of every stack
(function and file only), leak and error summaries.
Dropped: the banner, thread announcements, frames inside libc or Valgrind's
preload libraries below the innermost one, and DRD's "Thread N:" switch markers.
Scrubbed: every hex address, and with --anon-threads the thread numbers, whose
assignment depends on which thread the kernel happened to run first.

Usage: python3 -I digest.py [--anon-threads] <valgrind-log>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OURS = re.compile(r"(memcheck_bugs|thread_bugs)\.cpp:\d+")
PREFIX = re.compile(r"^==\d+== ?")
FRAME = re.compile(r"^(at|by) 0x[0-9A-Fa-f]+: (.*?)(?: \((.*)\))?$")
HEX = re.compile(r"0x[0-9A-Fa-f]+")
DROP = (
    "Memcheck, a memory error detector",
    "Helgrind, a thread error detector",
    "DRD, a thread error detector",
    "drd, a thread error detector",
    "Parent PID:",
    "Use --history-level=approx or =none",
    "the cost of reduced accuracy of conflicting-access information",
    "Copyright (C)",
    "Using Valgrind-",
    "Command: ",
    "For lists of detected and suppressed errors",
    "For a detailed leak analysis",
    "Rerun with --leak-check=full",
    "Use --track-origins=yes",
    "To see them, rerun with",
    "total heap usage:",
    "(thread finished, call stack no longer available)",
    "---Thread-Announcement---",
    "----------------------------------------------------------------",
)
THREAD_ANNOUNCE = re.compile(r"^Thread #\d+ (was created|is the program's root thread)")


def frame(line: str) -> str | None:
    m = FRAME.match(line)
    if not m:
        return line
    kw, fn, loc = m.group(1), m.group(2), m.group(3) or ""
    if OURS.search(loc):
        return f"{kw} {fn} ({loc})"
    if kw != "at":
        return None
    where = loc[3:] if loc.startswith("in ") else loc.split(":")[0]
    return f"at {fn} ({Path(where).name})" if where else f"at {fn}"


def blocks(lines: list[str]) -> list[list[str]]:
    out, cur = [], []
    for raw in lines:
        line = PREFIX.sub("", raw.rstrip("\n")).strip()
        if raw.startswith("--"):  # Valgrind's own warnings, e.g. --PID-- lines
            continue
        if not line:
            if cur:
                out.append(cur)
            cur = []
            continue
        cur.append(line)
    if cur:
        out.append(cur)
    return out


def digest(text: str, anon_threads: bool) -> list[str]:
    result: list[str] = []
    for block in blocks(text.splitlines()):
        if any(THREAD_ANNOUNCE.match(line) for line in block):
            continue
        kept = []
        for line in block:
            if any(line.startswith(d) or d in line for d in DROP):
                continue
            if re.fullmatch(r"Thread \d+:", line):
                continue
            f = frame(line)
            if f is None:
                continue
            f = HEX.sub("ADDR", f)
            f = re.sub(r"held during \d+ ms", "held during N ms", f)
            f = re.sub(r"(BSS|data) section of \S+/", r"\1 section of ", f)
            if anon_threads:
                f = re.sub(r"([Tt]hread #?)\d+", r"\1T", f)
            kept.append(f)
        if kept:
            result.extend(kept)
            result.append("")
    while result and not result[-1]:
        result.pop()
    return result


def main(argv: list[str]) -> int:
    anon = "--anon-threads" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if len(paths) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    for line in digest(Path(paths[0]).read_text(), anon):
        print(("  " + line) if line else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
