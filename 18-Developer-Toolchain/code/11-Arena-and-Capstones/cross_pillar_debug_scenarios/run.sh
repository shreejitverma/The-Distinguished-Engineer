#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export TZ=UTC
export PYTHONUNBUFFERED=1

python3 -W ignore "${SCRIPT_DIR}/runner.py"
