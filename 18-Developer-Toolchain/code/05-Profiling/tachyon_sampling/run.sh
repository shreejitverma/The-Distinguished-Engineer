#!/usr/bin/env bash
# Tracing (cProfile / profiling.tracing) versus sampling (profiling.sampling, Tachyon).
# Requirements: none gated; python3 on the recording platform (the harness's empty form is no requires line).
# The Tachyon sections need Python 3.15; older interpreters print "needs Python 3.15" lines instead.
# No sudo: the out-of-process section reports whether this host lets an unprivileged user attach.
# Override the interpreter with PYTHON=/path/to/python3.15 bash run.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
export LC_ALL=C
export PYTHONHASHSEED=0

# -I: isolated mode, so no PYTHON* variable or user site-packages changes the result.
"${PY}" -I "${SCRIPT_DIR}/lab.py" "${SCRIPT_DIR}/target.py"
