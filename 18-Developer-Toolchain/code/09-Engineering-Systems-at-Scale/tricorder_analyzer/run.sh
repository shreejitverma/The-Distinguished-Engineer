#!/usr/bin/env bash
# Standard-library Python 3.10 or later. No optional harness requirements.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
export PYTHONDONTWRITEBYTECODE=1
python3 lab.py
