#!/usr/bin/env bash
# Drill: diagnose and resolve a diamond version incompatibility with PubGrub.
# No harness requirement tokens: it runs on macOS and Linux as is, offline.
# It needs python3 >= 3.10 and uv >= 0.11.25 on PATH, and fails loudly without uv.
# uv 0.11.25 added scoped overrides (Stage 7); the expected output was recorded with uv 0.11.26.
# Everything is offline: the registry is a directory of metadata-only wheels built by make_wheels.py.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C PYTHONDONTWRITEBYTECODE=1

if ! command -v uv >/dev/null 2>&1; then
    echo "run.sh: uv is not on PATH; install uv >= 0.11.25 (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

python3 "${SCRIPT_DIR}/make_wheels.py" "${WORK}/wheels"
python3 "${SCRIPT_DIR}/uv_stage.py" symptom "${WORK}"
python3 "${SCRIPT_DIR}/drill.py" "${WORK}/fixes.json"
python3 "${SCRIPT_DIR}/uv_stage.py" verify "${WORK}" "${WORK}/fixes.json"
