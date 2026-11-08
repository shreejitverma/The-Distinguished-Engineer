#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
cd "${SCRIPT_DIR}"

"${CC:-cc}" -O2 -Wall -Wextra -std=c99 nfa_engine.c -o "${WORK_DIR}/nfa_engine"
"${WORK_DIR}/nfa_engine"

python3 benchmark.py
