#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

"${CC:-cc}" -O2 -Wall -Wextra -std=c99 nfa_engine.c -o nfa_engine
./nfa_engine
rm -f nfa_engine

python3 benchmark.py
