#!/usr/bin/env bash
# Leak isolation with tracemalloc snapshot diffs, allocator domains, and the cyclic GC.
# Runs on macOS and Linux as is, so it declares no requirements;
# ctypes reaches libc malloc and the CPython C API.
# -I ignores every PYTHON* variable (PYTHONTRACEMALLOC, PYTHONMALLOC, PYTHONDEVMODE) and the user site.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C

exec python3 -I "${SCRIPT_DIR}/leak_lab.py"
