#!/usr/bin/env bash
# This lab needs python3, cargo and a system libz.
# STYLE section 8 has no requirement token for a Rust toolchain, so there is no `requires:` line;
# a host without cargo fails below with a clear message instead of a skip.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C

for tool in python3 cargo rustc; do
    command -v "$tool" >/dev/null 2>&1 || { echo "mini_git lab needs '$tool' on PATH" >&2; exit 1; }
done

python3 "${SCRIPT_DIR}/test_minigit.py"
