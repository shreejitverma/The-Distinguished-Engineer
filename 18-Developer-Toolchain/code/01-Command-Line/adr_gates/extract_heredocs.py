#!/usr/bin/env python3
"""Write the named files from the note's `cat <<'EOF' > PATH` heredocs, byte for byte.

Usage: extract_heredocs.py NOTE DEST PATH...

Each PATH must be the target of exactly one heredoc in NOTE, so a renamed or duplicated
file in the note fails the lab instead of running stale code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEREDOC_RE = re.compile(r"^cat <<'EOF' > (\S+)\n(.*?)^EOF$", re.MULTILINE | re.DOTALL)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: extract_heredocs.py NOTE DEST PATH...", file=sys.stderr)
        return 64
    note, dest, wanted = Path(argv[0]), Path(argv[1]), argv[2:]
    bodies: dict[str, list[str]] = {}
    for m in HEREDOC_RE.finditer(note.read_text(encoding="utf-8")):
        bodies.setdefault(m.group(1), []).append(m.group(2))
    for path in wanted:
        found = bodies.get(path, [])
        if len(found) != 1:
            print(
                f"{note.name}: expected one heredoc for {path}, found {len(found)}",
                file=sys.stderr,
            )
            return 1
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(found[0], encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
