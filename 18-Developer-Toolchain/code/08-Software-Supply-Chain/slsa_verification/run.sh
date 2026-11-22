#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONHASHSEED=0
export PYTHONDONTWRITEBYTECODE=1

# The toy PKI shells out to the openssl CLI for ECDSA P-256 sign and verify.
command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }

python3 "${SCRIPT_DIR}/drill.py"
