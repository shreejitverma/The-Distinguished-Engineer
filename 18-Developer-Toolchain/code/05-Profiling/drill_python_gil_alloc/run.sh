#!/usr/bin/env bash
# Drill: a C extension that reacquires the GIL and allocates once per record.
# Needs python3 with its development headers (Python.h) and a C compiler.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

bash "${SCRIPT_DIR}/build.sh" "${WORK}/build"
python3 "${SCRIPT_DIR}/drill.py" "${WORK}/build"
