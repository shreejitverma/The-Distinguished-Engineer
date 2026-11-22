#!/usr/bin/env python3
"""Negative controls: break the hardened release workflow one way at a time and show the auditor notices.

Every mutation asserts that its target text exists, so a stale fixture fails loudly instead of
silently testing nothing.

Usage: mutate_check.py HARDENED_RELEASE_YML
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_workflows import audit_text  # noqa: E402

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
ATTEST = "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4.2.2"
PYPI = "pypa/gh-action-pypi-publish@7245906fb61fa33a13554f9d5fb856fe61758086 # v1.14.3"

MUTATIONS = [
    ("M1 unpin attest to a tag", ATTEST, "actions/attest@v4", "GHA002"),
    ("M2 top-level write-all", "permissions: {}\njobs:", "permissions: write-all\njobs:", "GHA001"),
    ("M3 build inside the publish job", "      - uses: " + PYPI,
     "      - run: python -m build\n      - uses: " + PYPI, "GHA007"),
    ("M4 cache inside the attest job", "      - uses: " + ATTEST,
     "      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6\n"
     "        with:\n          cache: pip\n      - uses: " + ATTEST, "GHA008"),
    ("M5 drop the pypi environment", "    environment: pypi\n", "", "GHA009"),
    ("M6 add a long-lived PyPI token", "      - uses: " + PYPI,
     "      - uses: " + PYPI + "\n        with:\n          password: ${{ secrets.PYPI_API_TOKEN }}", "GHA011"),
    ("M7 interpolate a commit message", "          python -m build\n",
     "          python -m build\n          echo ${{ github.event.head_commit.message }}\n", "GHA004"),
    ("M8 persist checkout credentials", "persist-credentials: false", "persist-credentials: true", "GHA005"),
    ("M9 remove the attest job", None, None, "GHA012"),
]


def mutate(text, old, new):
    if text.count(old) < 1:
        raise SystemExit("mutation target not found: %r" % old[:50])
    return text.replace(old, new, 1)


def main(argv) -> int:
    base = Path(argv[1]).read_text()
    clean = audit_text(base)
    print("baseline findings: %d" % len(clean))
    bad = 0
    for label, old, new, expect in MUTATIONS:
        if old is None:  # M9: cut the whole attest job and its needs edges
            start = base.index("  attest:\n")
            end = base.index("  publish-pypi:\n")
            text = base[:start] + base[end:]
            text = text.replace("needs: [build, attest]", "needs: build")
            text = text.replace("          path: dist/\n      - uses: actions/attest@", "          path: dist/\n      - uses: actions/attest@")
        else:
            text = mutate(base, old, new)
        fired = sorted({f.rule for f in audit_text(text)})
        ok = expect in fired
        bad += 0 if ok else 1
        print("%-34s expect %s -> fired %s %s" % (label, expect, ",".join(fired) or "none", "DETECTED" if ok else "MISSED"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
