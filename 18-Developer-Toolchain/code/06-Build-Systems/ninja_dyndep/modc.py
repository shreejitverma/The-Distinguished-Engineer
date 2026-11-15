"""Toy module compiler, standing in for a Fortran or C++20 modules compiler.

Usage: modc.py SRC OUT

A source line `module NAME` makes this source provide module NAME: the compiler
writes NAME.mod holding the source's `export` lines, and rewrites it only when its
content changes, so an edge marked `restat` sees an unchanged mtime.
A line `use NAME` requires NAME.mod to exist already; if it does not, compilation
fails, exactly as a Fortran compiler fails on a missing .mod file.
Every run appends one line to compile.log so the lab can show the execution order.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    lines = src.read_text().splitlines()
    provides = [ln.split()[1] for ln in lines if ln.startswith("module ")]
    uses = [ln.split()[1] for ln in lines if ln.startswith("use ")]
    exports = [ln for ln in lines if ln.startswith("export ")]

    with open("compile.log", "a") as log:
        log.write(f"compile {src.name}\n")

    for name in uses:
        mod = Path(f"{name}.mod")
        if not mod.exists():
            print(f"modc: {src.name}: error: module '{name}' not found ({mod} missing)")
            return 1

    for name in provides:
        mod = Path(f"{name}.mod")
        body = "\n".join(exports) + "\n"
        if not mod.exists() or mod.read_text() != body:
            mod.write_text(body)

    out.write_text(f"object for {src.name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
