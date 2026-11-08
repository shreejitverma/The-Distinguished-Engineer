"""Reduce one AddressSanitizer or LeakSanitizer report to its run-independent facts.

Usage: python3 summarize.py <stderr-file> <exit-status>

Addresses, PIDs, and stack frames change from run to run, so this prints only what a reader
needs to learn from a report: the error kind, the access, where the address lies relative to
its object, the shadow byte that fired and its legend name, and the LeakSanitizer totals.
"""

from __future__ import annotations

import re
import sys

ERROR_RE = re.compile(
    r"ERROR: (AddressSanitizer|LeakSanitizer): (.+?)(?: on (?:address |unknown address )?0x.*)?$"
)
ACCESS_RE = re.compile(r"^(READ|WRITE) of size (\d+)")
LOCATED_RE = re.compile(r"is located (.+)$")
FRAME_OBJ_RE = re.compile(r"^\s+(\[\d+, \d+\) '[^']+')")
FAULT_ROW_RE = re.compile(r"^=>\S+:.*\[([0-9a-f]{2})\]")
LEGEND_RE = re.compile(r"^\s+([A-Za-z][A-Za-z ]+):\s+((?:[0-9a-f]{2} ?)+)\s*$")
LEAK_RE = re.compile(r"^(Direct|Indirect) leak of (\d+) byte\(s\) in (\d+) object\(s\)")
SUMMARY_RE = re.compile(
    r"^SUMMARY: (\w+): (\d+ byte\(s\) leaked in \d+ allocation\(s\))"
)
NOTE_RES = (re.compile(r"(detect_leaks is not supported on this platform)"),)
ADDR_RE = re.compile(r"\s*\[0x[0-9a-f]+,0x[0-9a-f]+\)|\s*\(0x[0-9a-f]+\)")


def legend(lines: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for line in lines:
        m = LEGEND_RE.match(line)
        if m:
            for byte in m.group(2).split():
                names.setdefault(byte, m.group(1).strip())
    return names


def located(text: str) -> str:
    text = ADDR_RE.sub("", text)
    # Drop the source file of a global: it is a path that depends on where the lab is built.
    return re.sub(r" defined in '[^']*'", "", text)


def main() -> int:
    path, status = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    names = legend(lines)
    out = [f"exit status {status}"]
    for line in lines:
        bare = re.sub(r"^==\d+==", "", line)
        if m := ERROR_RE.search(bare):
            out.append(f"{m.group(1)}: {m.group(2)}")
        elif m := ACCESS_RE.match(bare):
            out.append(f"{m.group(1)} of size {m.group(2)}")
        elif m := LOCATED_RE.search(bare):
            out.append(f"located {located(m.group(1))}")
        elif m := FRAME_OBJ_RE.match(bare):
            out.append(f"frame object {m.group(1)}")
        elif m := FAULT_ROW_RE.match(bare):
            byte = m.group(1)
            out.append(
                f"shadow byte at the fault: {byte} ({names.get(byte, 'unknown')})"
            )
        elif m := LEAK_RE.match(bare):
            out.append(
                f"{m.group(1)} leak: {m.group(2)} bytes in {m.group(3)} object(s)"
            )
        elif m := SUMMARY_RE.match(bare):
            out.append(f"summary from {m.group(1)}: {m.group(2)}")
        else:
            for note in NOTE_RES:
                if m := note.search(bare):
                    out.append(m.group(1))
    for line in out:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
