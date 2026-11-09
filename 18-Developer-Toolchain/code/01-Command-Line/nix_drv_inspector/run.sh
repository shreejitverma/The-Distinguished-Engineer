#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C

python3 "${SCRIPT_DIR}/inspector.py" --test-suite
