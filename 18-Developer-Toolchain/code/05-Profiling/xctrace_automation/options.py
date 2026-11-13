"""Write variants of the JSON that `xctrace record --show-recording-options` printed.

`--recording-options` wants the whole document back with values changed, so two variants are the
full document with one Time Profiler key flipped, and the third holds only that key, which
xctrace must refuse.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

INSTRUMENT = "Time Profiler"


def main(argv: list[str]) -> int:
    work = Path(argv[1])
    options = json.loads((work / "options.json").read_text())
    for key, name in (("highFrequencySampling", "high-frequency"), ("recordWaitingThreads", "waiting-threads")):
        if options[INSTRUMENT][key] is not False:
            raise SystemExit(f"options: {key} is not false by default: {options[INSTRUMENT][key]!r}")
        variant = copy.deepcopy(options)
        variant[INSTRUMENT][key] = True
        (work / f"options-{name}.json").write_text(json.dumps(variant, indent=2))
    partial = {INSTRUMENT: {"highFrequencySampling": True}}
    (work / "options-partial.json").write_text(json.dumps(partial, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
