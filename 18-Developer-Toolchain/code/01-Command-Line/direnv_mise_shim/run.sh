#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONHASHSEED=0

python3 "${SCRIPT_DIR}/shim_engine.py"
