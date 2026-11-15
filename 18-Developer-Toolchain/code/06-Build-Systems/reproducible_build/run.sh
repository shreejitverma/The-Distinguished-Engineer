#!/usr/bin/env bash
# Hermetic and Reproducible Builds lab: build one small C and Python project twice under
# Debian-style variations (path, clock, TZ, locale, hash seed, umask, checkout mtimes),
# then apply the standard fixes one at a time and explain every remaining difference.
# No requires line: it runs on macOS as is (Apple clang, cctools ar, ld64) and on Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export CC="${CC:-cc}"
python3 "${SCRIPT_DIR}/repro.py"
