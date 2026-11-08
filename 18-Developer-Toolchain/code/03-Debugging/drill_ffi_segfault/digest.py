"""Reduce faulthandler and AddressSanitizer reports to address-free facts.

Usage:
    python3 digest.py faulthandler STDERR_FILE EXIT_STATUS
    python3 digest.py asan STDERR_FILE EXIT_STATUS
"""

from __future__ import annotations

import os
import re
import sys

PY_FRAME_RE = re.compile(r'^\s+File "([^"]+)", line (\d+) in (\S+)', re.MULTILINE)
FRAME_RE = re.compile(r"^\s+#\d+ 0x[0-9a-fA-F]+ (.*)$")
LIBRS_RE = re.compile(r"tape_sum\b.*?([^\s/()]*lib\.rs):(\d+)")


def yn(flag: bool) -> str:
    return "yes" if flag else "no"


def status_text(status: int) -> str:
    if status > 128:
        return f"{status} (128 + signal {status - 128})"
    return str(status)


def faulthandler(text: str, status: int) -> None:
    print(f"exit status: {status_text(status)}")
    banner = re.search(r"^Fatal Python error: (.+)$", text, re.MULTILINE)
    print(f"faulthandler banner: {banner.group(0) if banner else 'none'}")
    # Only the faulting thread's block: it ends at the first blank line.
    block = (
        text.split("most recent call first):", 1)[1].split("\n\n", 1)[0]
        if banner
        else ""
    )
    frames = PY_FRAME_RE.findall(block)
    if frames:
        path, line, func = frames[0]
        print(f"innermost Python frame: {os.path.basename(path)}:{line} in {func}")
        print("Python call chain: " + " <- ".join(f[2] for f in frames))
    else:
        print("innermost Python frame: none")
    c_stack = "C stack trace" in text
    print(f"C stack section present (Python 3.14+): {yn(c_stack)}")
    if c_stack:
        print(f"C stack names tape_sum: {yn('tape_sum' in text)}")


def stack_after(lines: list[str], header: str) -> list[str]:
    """Frame lines of the stack that follows the first line containing header."""
    for i, line in enumerate(lines):
        if header in line:
            out = []
            for later in lines[i + 1 :]:
                m = FRAME_RE.match(later)
                if not m:
                    break
                out.append(m.group(1))
            return out
    return []


def is_rust(frame: str) -> bool:
    return "libticktape" in frame or "tape_" in frame or "ticktape" in frame


def asan(text: str, status: int) -> None:
    head = re.search(r"ERROR: AddressSanitizer: (\S+)", text)
    if not head:
        print(f"ASan report: none (exit status {status_text(status)})")
        return
    print(f"ASan report: {head.group(1)}")
    access = re.search(r"^(READ|WRITE) of size (\d+)", text, re.MULTILINE)
    print(
        f"access: {access.group(1)} of size {access.group(2)}"
        if access
        else "access: not reported"
    )
    region = re.search(r"is located (\d+) bytes inside of (\d+)-byte region", text)
    if region:
        print(
            f"address: {region.group(1)} bytes inside a {region.group(2)}-byte heap block"
        )
    lines = text.splitlines()
    use = stack_after(lines, "of size")
    at = next((LIBRS_RE.search(f) for f in use if LIBRS_RE.search(f)), None)
    print(
        f"use stack reaches tape_sum: {yn(any('tape_sum' in f for f in use))}"
        + (f" ({at.group(1).rsplit('/', 1)[-1]}:{at.group(2)})" if at else "")
    )
    print(f"use stack crosses libffi: {yn(any('ffi' in f for f in use))}")
    freed = stack_after(lines, "freed by thread")
    alloc = stack_after(lines, "previously allocated by thread")
    if freed:
        print(
            f"freed by: {'free()' if 'free' in freed[0] else freed[0].split()[0]}, "
            f"Rust frames in that stack: {sum(map(is_rust, freed))}"
        )
    if alloc:
        print(
            f"allocated by: {'malloc()' if 'alloc' in alloc[0] else alloc[0].split()[0]}, "
            f"Rust frames in that stack: {sum(map(is_rust, alloc))}"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("faulthandler", "asan"):
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[2], errors="replace") as fh:
        text = fh.read()
    status = int(argv[3])
    (faulthandler if argv[1] == "faulthandler" else asan)(text, status)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
