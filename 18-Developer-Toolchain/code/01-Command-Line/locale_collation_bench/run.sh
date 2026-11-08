#!/usr/bin/env bash
# requires: macos
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_PATH="${SCRIPT_DIR}/locale_bench"

cleanup() {
    rm -f "${BIN_PATH}"
}
trap cleanup EXIT

# Compile the C benchmark and inspection utility
"${CC:-cc}" -O2 -Wall -Wextra "${SCRIPT_DIR}/locale_bench.c" -o "${BIN_PATH}"

# Execute C inspection binary
"${BIN_PATH}"

# Execute pipeline and filesystem tests
python3 "${SCRIPT_DIR}/pipeline_test.py"
