"""A toy compiler and linker that behave like cc for the build engine.

  toycc.py compile SRC -o OUT -MF DEPFILE [-I DIR]... [--edit-during FILE]
  toycc.py link OBJ... -o OUT
  toycc.py stamp -o OUT

`compile` expands #include "name" recursively (directory of the including
file first, then each -I directory), drops // comments and blank lines, and
writes a GCC-style depfile with -MP phony rules.
It also reads the environment variable TOYCC_OPT and embeds it in the output,
which is the untracked dependency the lab exposes.
`--edit-during FILE` appends a line to FILE after reading the inputs: a fault
injection that stands in for an editor saving a file mid-compile.
`stamp` writes the current time unless SOURCE_DATE_EPOCH is set.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from depfile import escape  # noqa: E402


def die(msg: str) -> None:
    sys.stderr.write(f"toycc: error: {msg}\n")
    sys.exit(1)


def expand(path: str, incdirs: list[str], seen: list[str], stack: list[str]) -> list[str]:
    if path in stack:
        die(f"include cycle through {path}")
    if path not in seen:
        seen.append(path)
    out: list[str] = []
    with open(path, encoding="ascii") as f:
        lines = f.read().splitlines()
    for line in lines:
        s = line.strip()
        if s.startswith("#include"):
            name = s.split('"')[1]
            cands = [os.path.join(os.path.dirname(path), name)] + [os.path.join(d, name) for d in incdirs]
            hit = next((c for c in cands if os.path.isfile(c)), None)
            if hit is None:
                die(f"{path}: '{name}' file not found")
            out += expand(os.path.normpath(hit), incdirs, seen, stack + [path])
            continue
        code = line.split("//", 1)[0].rstrip()
        if code:
            out.append(code)
    return out


def compile_(argv: list[str]) -> None:
    src = argv[0]
    out = argv[argv.index("-o") + 1]
    dep = argv[argv.index("-MF") + 1]
    incdirs = [argv[k + 1] for k, a in enumerate(argv) if a == "-I"]
    edit_during = argv[argv.index("--edit-during") + 1] if "--edit-during" in argv else None
    seen: list[str] = []
    body = expand(src, incdirs, seen, [])
    if edit_during:
        with open(edit_during, "a", encoding="ascii") as f:
            f.write("// saved by an editor during the compile\n")
    opt = os.environ.get("TOYCC_OPT", "-")
    with open(out, "w", encoding="ascii") as f:
        f.write(f"OBJ toycc-1 opt={opt}\n")
        f.writelines(f"  {line}\n" for line in body)
    with open(dep, "w", encoding="ascii") as f:
        f.write(escape(out) + ": " + " \\\n  ".join(escape(p) for p in seen) + "\n")
        for p in seen[1:]:
            f.write(escape(p) + ":\n")


def link(argv: list[str]) -> None:
    out = argv[argv.index("-o") + 1]
    objs = argv[: argv.index("-o")]
    with open(out, "w", encoding="ascii") as f:
        f.write("APP toycc-1\n")
        for o in objs:
            with open(o, encoding="ascii") as g:
                f.write(g.read())


def stamp(argv: list[str]) -> None:
    out = argv[argv.index("-o") + 1]
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    when = sde if sde is not None else str(time.time_ns())
    with open(out, "w", encoding="ascii") as f:
        f.write(f"OBJ stamp built_at={when}\n")


def main() -> None:
    cmd, rest = sys.argv[1], sys.argv[2:]
    {"compile": compile_, "link": link, "stamp": stamp}[cmd](rest)


if __name__ == "__main__":
    main()
