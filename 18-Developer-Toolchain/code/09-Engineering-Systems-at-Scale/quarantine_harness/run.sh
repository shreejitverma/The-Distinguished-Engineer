#!/usr/bin/env bash
# Needs nothing beyond python3 and bash; it runs on macOS and Linux as is.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONHASHSEED=0
export PYTHONDONTWRITEBYTECODE=1
unset TEST_SEED
cd "${SCRIPT_DIR}"
python3 harness.py
