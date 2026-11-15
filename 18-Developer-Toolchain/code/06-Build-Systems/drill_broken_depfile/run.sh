#!/usr/bin/env bash
# Drill: Fix a Broken Incremental Depfile Generator.
# Runs every failure of schemagen_broken.py in isolation, then the same scenario with the fix.
# Needs ninja >= 1.11 (for -t missingdeps), cmake >= 3.29, a C compiler, and python3.
# No requires line: it runs as is on macOS and on Linux hosts that have these tools.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "${SCRATCH}"' EXIT

for tool in ninja cmake python3 "${CC:-cc}"; do
    command -v "${tool}" >/dev/null || { echo "missing required tool: ${tool}" >&2; exit 1; }
done
python3 - "$(ninja --version)" <<'PY'
import sys
v = tuple(int(x) for x in sys.argv[1].split(".")[:2])
if v < (1, 11):
    sys.exit(f"ninja {sys.argv[1]} is older than 1.11, which added -t missingdeps")
PY

echo "Lab: broken versus fixed depfile generator (ninja >= 1.11 confirmed)"
python3 "${SCRIPT_DIR}/drill.py" "${SCRIPT_DIR}" "${SCRATCH}"
