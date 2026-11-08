#!/usr/bin/env bash
# requires: macos
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

export LC_ALL=C
python3 "${SCRIPT_DIR}/verify_remarks.py"
