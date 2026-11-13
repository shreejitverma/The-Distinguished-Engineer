#!/usr/bin/env bash
# Drill: coordinated omission in a load generator.
# Runs three generators against one simulated stalling service in virtual time,
# so the output is byte-identical on every run and every platform.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

python3 "${SCRIPT_DIR}/drill.py" "$@"
