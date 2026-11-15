"""Copy the drill project into a work tree and write its 22 repetitive translation units.

    python3 gen_tree.py PROJECT_DIR OUT_DIR {umbrella,precise}

umbrella: every generated TU includes "core/core.h", which pulls in the generated schema.h.
precise:  every generated TU includes only what it uses ("core/util.h"; main.cpp also
          "core/serialize.h"), so only core/serialize.cpp includes the generated header.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

CORE_TUS = 5
NET_TUS = 8
RISK_TUS = 8


def tu(lib: str, index: int, include_mode: str) -> str:
    header = "core/core.h" if include_mode == "umbrella" else "core/util.h"
    return (
        f'#include "{header}"\n'
        f"namespace {lib} {{\n"
        f"int {lib}_{index}(int x) {{ return core::checksum(x + {index}); }}\n"
        f"}}  // namespace {lib}\n"
    )


def main_cpp(include_mode: str) -> str:
    if include_mode == "umbrella":
        includes = '#include "core/core.h"\n'
    else:
        includes = '#include "core/serialize.h"\n#include "core/util.h"\n'
    decls = "".join(f"namespace net {{ int net_{i}(int); }}\n" for i in range(NET_TUS))
    decls += "".join(f"namespace risk {{ int risk_{i}(int); }}\n" for i in range(RISK_TUS))
    calls = " + ".join([f"net::net_{i}(1)" for i in range(NET_TUS)] + [f"risk::risk_{i}(1)" for i in range(RISK_TUS)])
    return (
        includes
        + decls
        + "int main() {\n"
        + f"  int sum = {calls};\n"
        + "  return (sum + core::serialized_field_count()) == 0;\n"
        + "}\n"
    )


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[3] not in ("umbrella", "precise"):
        print(__doc__, file=sys.stderr)
        return 2
    project, out, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    shutil.copytree(project, out)
    for i in range(CORE_TUS):
        (out / "core" / f"core_{i}.cpp").write_text(tu("core", i, mode))
    for lib, count in (("net", NET_TUS), ("risk", RISK_TUS)):
        (out / lib).mkdir()
        for i in range(count):
            (out / lib / f"{lib}_{i}.cpp").write_text(tu(lib, i, mode))
    (out / "app").mkdir()
    (out / "app" / "main.cpp").write_text(main_cpp(mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
