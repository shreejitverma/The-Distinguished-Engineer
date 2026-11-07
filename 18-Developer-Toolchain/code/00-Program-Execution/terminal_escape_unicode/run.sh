#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CC="${CC:-cc}"
BIN="./terminal_engine"

trap 'rm -f "${BIN}"' EXIT

"${CC}" -O2 -Wall -Wextra -pedantic -std=c99 terminal_engine.c -o "${BIN}"
"${BIN}"
