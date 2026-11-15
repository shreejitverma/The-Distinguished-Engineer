"""Toy dependency scanner that writes a Ninja dyndep file.

Usage: modscan.py OUT.dd SRC...

For each SRC it assumes the object is SRC with the suffix .o, records the modules
the source provides as implicit outputs and the modules it uses as implicit inputs,
and marks providers `restat = 1` so an unchanged .mod file stops the rebuild there.
This is what `gfortran`-aware scanners and P1689 C++ module scanners do for real.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    out = Path(sys.argv[1])
    rows = ["ninja_dyndep_version = 1"]
    for arg in sys.argv[2:]:
        src = Path(arg)
        lines = src.read_text().splitlines()
        provides = [f"{ln.split()[1]}.mod" for ln in lines if ln.startswith("module ")]
        uses = [f"{ln.split()[1]}.mod" for ln in lines if ln.startswith("use ")]
        obj = src.with_suffix(".o").name
        imp_outs = (" | " + " ".join(provides)) if provides else ""
        imp_ins = (" | " + " ".join(uses)) if uses else ""
        rows.append(f"build {obj}{imp_outs}: dyndep{imp_ins}")
        if provides:
            rows.append("  restat = 1")
    with open("compile.log", "a") as log:
        log.write("scan " + " ".join(Path(a).name for a in sys.argv[2:]) + "\n")
    out.write_text("\n".join(rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
