"""Code generator for gen/opcodes.h, standing in for a protocol or IDL compiler.

The opcode table is a set. Iterating a set of str follows the str hash, and CPython
randomizes str hashing per process (PYTHONHASHSEED), so the unsorted mode emits the
opcodes in an order that changes between runs. --sorted emits them in sorted order.
"""

from __future__ import annotations

import sys

OPCODES = {
    "ADD", "SUB", "MUL", "DIV", "LOAD", "STORE",
    "JUMP", "CALL", "RET", "PUSH", "POP", "HALT",
}


def render(names: list[str]) -> str:
    lines = ["#pragma once", "", "enum class Op : unsigned char {"]
    lines += [f"    {name} = {i}," for i, name in enumerate(names)]
    lines += ["};", ""]
    quoted = ", ".join(f'"{name}"' for name in names)
    lines += [f"inline const char* const kOpNames[] = {{{quoted}}};", ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    out = argv[1]
    names = sorted(OPCODES) if "--sorted" in argv[2:] else list(OPCODES)
    with open(out, "w", encoding="ascii") as f:
        f.write(render(names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
