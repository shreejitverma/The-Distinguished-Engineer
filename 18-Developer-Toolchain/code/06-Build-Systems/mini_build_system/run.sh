#!/usr/bin/env bash
# Rebuild lab: a content-hashed build engine (suspending scheduler, constructive
# traces, early cutoff, depfile dependencies) driven through 19 scenarios.
# Needs python3 (3.11 or later, for hashlib.file_digest) and make on PATH.
# It declares no requirements: it runs as is on macOS and Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=0

command -v make >/dev/null || { echo "make not found" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "python3 3.11 or later is required (hashlib.file_digest)" >&2; exit 1; }

python3 "${SCRIPT_DIR}/scenarios.py"
