#!/usr/bin/env python3
"""Count LLVM inlining remarks that match a pattern.

`rustc -Cremark=inline -Cdebuginfo=1` makes LLVM's inliner report every decision on stderr:

    note: <file>:<line>:<col> inline (success): '<callee>' inlined into '<caller>' with (cost=..., threshold=...)
    note: <file>:<line>:<col> inline (missed): <callee> will not be inlined into <caller> because its definition is unavailable

This script demangles the symbols, replaces generic argument lists with `<_>` the way
llvm_lines.py does, drops the source location, quotes, and cost figures (which move with
every LLVM release), and prints how many remarks match the given regular expression.

Usage: remarks.py REMARKS.txt REGEX
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from llvm_lines import strip_generic_args


def normalized(path: Path) -> list[str]:
    raw = [
        line
        for line in path.read_text(errors="replace").splitlines()
        if " inline (" in line
    ]
    if not raw:
        return []
    proc = subprocess.run(
        ["c++filt"],
        input="\n".join(raw) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    out = []
    for line in proc.stdout.splitlines():
        line = line[line.index("inline (") :].replace("'", "")
        line = re.sub(r" with \(cost=.*| at callsite .*| \(cost=.*", "", line)
        out.append(strip_generic_args(line))
    return out


def main(argv: list[str]) -> int:
    lines = normalized(Path(argv[0]))
    if not lines:
        raise SystemExit(
            f"{argv[0]} holds no inline remarks: was the crate rebuilt with -Cremark=inline?"
        )
    pattern = re.compile(argv[1])
    print(sum(1 for line in lines if pattern.search(line)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
