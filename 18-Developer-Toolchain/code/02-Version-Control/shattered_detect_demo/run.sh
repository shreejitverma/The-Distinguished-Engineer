#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${SCRIPT_DIR}/demo_bin"
trap 'rm -f "${BIN}"' EXIT

"${CC:-cc}" -O2 -Wall -Wextra -Werror -std=c99 \
    "${SCRIPT_DIR}/main.c" "${SCRIPT_DIR}/sha1dc/sha1.c" "${SCRIPT_DIR}/sha1dc/ubc_check.c" -o "${BIN}"
"${BIN}"
