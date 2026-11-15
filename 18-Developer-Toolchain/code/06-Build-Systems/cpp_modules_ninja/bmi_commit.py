#!/usr/bin/env python3
"""Move each freshly written `<bmi>.tmp` named in a module map over `<bmi>`, unless the bytes match.

This is the "BMI Modification Optimization" that cmake-cxxmodules(7) lists as a possible
future enhancement: compilers always rewrite the BMI, so a compile edge marked `restat = 1`
can only prune its importers if the old file, and therefore its mtime, is left in place
when the new bytes are identical.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    modmap = Path(sys.argv[1])
    for line in modmap.read_text().splitlines():
        if not line.startswith("-fmodule-output="):
            continue
        tmp = Path(line.split("=", 1)[1])
        real = tmp.with_suffix("")  # strip ".tmp"
        if real.exists() and real.read_bytes() == tmp.read_bytes():
            tmp.unlink()
        else:
            os.replace(tmp, real)
    return 0


if __name__ == "__main__":
    sys.exit(main())
