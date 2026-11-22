#!/usr/bin/env python3
"""Rewrite 'uses: owner/repo@ref' to 'uses: owner/repo@<40-hex sha> # version' from an offline map.

The map (pins.json) is produced by `git ls-remote` against the action's own repository; this tool
never touches the network. References that are already a 40-hex SHA, local (./), or docker:// are
left alone. A reference missing from the map is reported and makes the exit status 2.

Usage: pin_actions.py PINS_JSON WORKFLOW...
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

USES = re.compile(r"^(?P<lead>\s*(?:-\s+)?uses:\s*)(?P<name>[^\s#@]+)@(?P<ref>[^\s#]+)(?P<tail>\s*#.*)?$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def pin_text(text: str, pins: dict):
    out, changed, unresolved = [], [], []
    for line in text.splitlines():
        m = USES.match(line)
        if not m or m["name"].startswith(("./", "docker://")) or SHA40.match(m["ref"]):
            out.append(line)
            continue
        # Composite actions in a subdirectory (owner/repo/path) resolve by owner/repo.
        repo = "/".join(m["name"].split("/")[:2])
        entry = pins.get("%s@%s" % (repo, m["ref"]))
        if entry is None:
            unresolved.append("%s@%s" % (m["name"], m["ref"]))
            out.append(line)
            continue
        new = "%s%s@%s # %s" % (m["lead"], m["name"], entry["sha"], entry["version"])
        changed.append((m["name"], m["ref"], entry["sha"], entry["version"]))
        out.append(new)
    return "\n".join(out) + "\n", changed, unresolved


def main(argv) -> int:
    pins = json.loads(Path(argv[1]).read_text())
    status = 0
    for name in argv[2:]:
        path = Path(name)
        new, changed, unresolved = pin_text(path.read_text(), pins)
        path.write_text(new)
        print("%s: pinned %d, unresolved %d" % (path.name, len(changed), len(unresolved)))
        for n, ref, sha, ver in changed:
            print("  %s@%s -> %s... (%s)" % (n, ref, sha[:12], ver))
        for u in unresolved:
            print("  UNRESOLVED %s" % u)
            status = 2
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
