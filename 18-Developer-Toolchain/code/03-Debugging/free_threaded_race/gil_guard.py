"""Watch one import turn the GIL back on in a free-threaded interpreter.

run.sh puts the compiled `ready` and `legacy` modules on sys.path and runs this
file with PYTHON_GIL unset, then with PYTHON_GIL=0.
`ready` declares Py_mod_gil = Py_MOD_GIL_NOT_USED; `legacy` has no Py_mod_gil slot.
After the imports, workload A from race.py shows whether threads can still lose updates.
"""

from __future__ import annotations

import importlib
import os
import sys
import warnings

import race


def gil_state() -> str:
    return "enabled" if sys._is_gil_enabled() else "disabled"


def import_and_report(name: str, label: str) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module(name)
    print(f"  import {name:<6} ({label}): GIL {gil_state()}, warnings: {len(caught)}")
    for w in caught:
        print(f"    {w.category.__name__}: {w.message}")


def main() -> int:
    print(f"PYTHON_GIL={os.environ.get('PYTHON_GIL', '(unset)')}: GIL at startup {gil_state()}")
    import_and_report("ready", "declares Py_MOD_GIL_NOT_USED")
    import_and_report("legacy", "no Py_mod_gil slot")
    print(f"  workload A after both imports, lost updates: {race.verdict(race.inc_plain, race.TRIALS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
