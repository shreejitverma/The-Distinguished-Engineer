#!/usr/bin/env python3
"""Count LLVM IR lines and copies per generic function: a small, offline cargo-llvm-lines.

Input is the unoptimized IR that `rustc --emit=llvm-ir -Cno-prepopulate-passes` writes, which
is what rustc hands to LLVM before any LLVM pass has run. The counting rule is the one
cargo-llvm-lines uses (src/count.rs): a function starts at a line beginning with `define `,
ends at a line that is exactly `}`, and each line in between that starts with exactly two
spaces is one IR line.

The difference is the grouping key. cargo-llvm-lines 0.4.48 demangles the symbol and strips
a trailing legacy `::h<16 hex digits>` hash, which merges instantiations only under the legacy
mangling scheme, where the generic arguments are not part of the name. Rust 1.97 made the v0
scheme the default, and a v0 name spells out every generic argument, so each instantiation
is its own row with Copies = 1. This script demangles with c++filt and then replaces every
generic argument list with `<_>`, so `kit::ingest::<Nyse, Vwap>` and `kit::ingest::<Cme,
Range>` land in one row again.

Usage: llvm_lines.py [--top N] [--total-only] [--copies NAME] [--ungrouped-rows NAME] [--mangling]
                     FILE.ll [FILE.ll ...]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

LEGACY_HASH = re.compile(r"::h[0-9a-f]{16}$")


def functions(ir: str) -> list[tuple[str, int]]:
    """Return (mangled name, IR line count) for every function defined in one .ll file."""
    out: list[tuple[str, int]] = []
    current: str | None = None
    count = 0
    for line in ir.splitlines():
        if line.startswith("define "):
            start = line.find("@")
            end = line.find("(", start)
            current = (
                line[start + 1 : end].strip('"') if start != -1 and end != -1 else None
            )
            count = 0
        elif line == "}":
            if current is not None:
                out.append((current, count))
            current = None
            count = 0
        elif line.startswith("  ") and not line.startswith("   "):
            count += 1
    return out


def demangle(names: list[str]) -> list[str]:
    """Demangle with c++filt, which understands the Rust v0 scheme; one process for all names."""
    if not names:
        return []
    proc = subprocess.run(
        ["c++filt"],
        input="\n".join(names) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    lines = proc.stdout.splitlines()
    if len(lines) != len(names):
        raise SystemExit(f"c++filt returned {len(lines)} lines for {len(names)} names")
    return lines


def strip_generic_args(name: str) -> str:
    """Replace every generic argument list with `<_>`, keeping `<T as Trait>` qualifiers.

    A `<` that follows an identifier character or `::` opens generic arguments; a `<` at the
    start of the name or after any other character opens a qualified path such as
    `<kit::Pipeline<Nyse>>::run` or `<u8 as core::fmt::Debug>::fmt`, which stays.
    """
    out: list[str] = []
    i = 0
    n = len(name)
    while i < n:
        ch = name[i]
        if ch == "<" and out and (out[-1].isalnum() or out[-1] in "_:"):
            depth = 0
            while i < n:
                if name[i] == "<":
                    depth += 1
                elif name[i] == ">" and name[i - 1] != "-":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if out[-1] == ":":
                del out[-2:]
            out.append("<_>")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--top", type=int, default=8, help="rows to print (default 8)")
    ap.add_argument(
        "--total-only",
        action="store_true",
        help="print `<lines> <functions>` and nothing else",
    )
    ap.add_argument(
        "--copies",
        metavar="NAME",
        help="print only the copy count of this grouped name",
    )
    ap.add_argument(
        "--ungrouped-rows",
        metavar="NAME",
        help="print how many rows cargo-llvm-lines 0.4.48 shows for this grouped name",
    )
    ap.add_argument(
        "--mangling",
        action="store_true",
        help="print the symbol mangling scheme in use",
    )
    args = ap.parse_args(argv)

    found: list[tuple[str, int]] = []
    for path in args.files:
        found.extend(functions(path.read_text(errors="replace")))
    total_lines = sum(lines for _, lines in found)

    if args.total_only:
        print(total_lines, len(found))
        return 0

    if args.mangling:
        v0 = sum(1 for name, _ in found if name.startswith("_R"))
        legacy = sum(1 for name, _ in found if name.startswith("_ZN"))
        print("v0" if v0 > legacy else "legacy")
        return 0

    demangled = demangle([m for m, _ in found])

    if args.ungrouped_rows:
        # The cargo-llvm-lines 0.4.48 key: the demangled name minus a trailing legacy hash.
        rows = {
            LEGACY_HASH.sub("", d)
            for d in demangled
            if strip_generic_args(d) == args.ungrouped_rows
        }
        print(len(rows))
        return 0

    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for name, (_, lines) in zip(demangled, found):
        entry = groups[strip_generic_args(name)]
        entry[0] += lines
        entry[1] += 1

    if args.copies:
        print(groups[args.copies][1] if args.copies in groups else 0)
        return 0

    print(f"  {'Lines':>6}  {'Copies':>6}  Function name")
    print(f"  {'-----':>6}  {'------':>6}  -------------")
    print(f"  {total_lines:>6}  {len(found):>6}  (TOTAL)")
    # Ties broken by name so the table is stable from run to run.
    ranked = sorted(groups.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
    for name, (lines, copies) in ranked[: args.top]:
        print(f"  {lines:>6}  {copies:>6}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
