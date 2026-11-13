#!/usr/bin/env bash
# Coordinated omission, HdrHistogram bucket internals, and robust benchmark statistics.
# No requirement line: needs only python3 (3.9 or later, standard library only), runs on macOS and Linux as is.
# Every number is simulated in integer microseconds or drawn from a seeded PRNG, so the output is deterministic.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONHASHSEED=0

# -B: write no __pycache__ into the lab; -s: ignore user site-packages. Not -I, whose -P would
# drop the script directory from sys.path and break `from hdr import Histogram`.
python3 -B -s "${SCRIPT_DIR}/lab.py"
