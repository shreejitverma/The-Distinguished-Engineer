#!/usr/bin/env bash
# PubGrub, version SAT, and minimal version selection, checked against uv, pip, and go.
# Harness requirements: none (runs on macOS as is).
# Needs python3 (3.9+, with venv and ensurepip), uv, and go on PATH; no network.
# The from-scratch solvers use the standard library only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export PYTHONDONTWRITEBYTECODE=1 LC_ALL=C

for tool in python3 uv go; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        echo "run.sh: ${tool} not found on PATH; this lab cross-checks against it" >&2
        exit 1
    fi
done

python3 "${SCRIPT_DIR}/lab.py" "${WORK}/mvs.json"
PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/crosscheck.py" "${WORK}" "${WORK}/mvs.json"
