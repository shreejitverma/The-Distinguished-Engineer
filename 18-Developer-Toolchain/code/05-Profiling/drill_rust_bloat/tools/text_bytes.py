#!/usr/bin/env python3
"""Attribute a release binary's machine code to grouped function names: a small cargo-bloat.

Sizes come from the symbol table: a function's size is the distance from its address to the
next symbol's address, which is how tools size Mach-O functions, where the symbol table
carries no size field. The grouping is the one llvm_lines.py uses, so a row here lines up
with a row there: `Copies` in the IR table is how many instantiations rustc sent to LLVM,
`Copies` here is how many survived inlining and MergeFunctions as separate functions.

Usage: text_bytes.py [--top N] [--only REGEX] [--sum REGEX] BINARY
"""

from __future__ import annotations

import argparse
import platform
import re
import subprocess
import sys
from collections import defaultdict

from llvm_lines import demangle, strip_generic_args


def text_section_bytes(binary: str) -> int:
    """Size of the main code section: __TEXT,__text on Mach-O, .text on ELF."""
    if platform.system() == "Darwin":
        out = subprocess.run(
            ["size", "-m", binary], capture_output=True, text=True, check=True
        ).stdout
        m = re.search(r"Section __text: (\d+)", out)
    else:
        out = subprocess.run(
            ["size", "-A", binary], capture_output=True, text=True, check=True
        ).stdout
        m = re.search(r"^\.text\s+(\d+)", out, re.MULTILINE)
    if not m:
        raise SystemExit(f"could not find the text section size of {binary}")
    return int(m.group(1))


def function_sizes(binary: str) -> list[tuple[str, int]]:
    """(demangled name, bytes) for every function symbol, sized by the next symbol's address."""
    out = subprocess.run(
        ["nm", "-n", binary], capture_output=True, text=True, check=True
    ).stdout
    symbols: list[tuple[int, str, str]] = []
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and re.fullmatch(r"[0-9a-fA-F]+", parts[0]):
            symbols.append((int(parts[0], 16), parts[1], parts[2]))
    mangled: list[str] = []
    sizes: list[int] = []
    # zip, not itertools.pairwise: the python3 that ships with Xcode is 3.9.
    for (addr, kind, name), (next_addr, _, _) in zip(symbols, symbols[1:]):  # noqa: RUF007
        if kind in "tT" and next_addr > addr:
            # Mach-O prefixes every C-level symbol with one underscore; c++filt wants it gone.
            mangled.append(
                name[1:]
                if platform.system() == "Darwin" and name.startswith("_")
                else name
            )
            sizes.append(next_addr - addr)
    return list(zip(demangle(mangled), sizes))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("binary")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument(
        "--only", default=".", help="keep groups whose name matches this regex"
    )
    ap.add_argument(
        "--sum", help="print `<copies> <bytes>` summed over groups matching this regex"
    )
    ap.add_argument(
        "--section",
        action="store_true",
        help="print the text section size and nothing else",
    )
    args = ap.parse_args(argv)

    if args.section:
        print(text_section_bytes(args.binary))
        return 0

    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for name, size in function_sizes(args.binary):
        entry = groups[strip_generic_args(name)]
        entry[0] += size
        entry[1] += 1

    if args.sum:
        picked = [v for k, v in groups.items() if re.search(args.sum, k)]
        print(sum(v[1] for v in picked), sum(v[0] for v in picked))
        return 0

    keep = re.compile(args.only)
    ranked = sorted(
        ((k, v) for k, v in groups.items() if keep.search(k)),
        key=lambda kv: (-kv[1][0], kv[0]),
    )
    print(f"  {'Bytes':>7}  {'Copies':>6}  Function name")
    print(f"  {'-----':>7}  {'------':>6}  -------------")
    for name, (size, copies) in ranked[: args.top]:
        print(f"  {size:>7}  {copies:>6}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
