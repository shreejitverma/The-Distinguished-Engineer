#!/usr/bin/env bash
# Drill lab: audit a gamed developer productivity dashboard.
# Pure Python 3 standard library, no network, no requirements beyond python3.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONHASHSEED=0

# -I: isolated mode, so nothing in the current directory can shadow a stdlib module.
python3 -I "${SCRIPT_DIR}/dashboard_audit.py"
