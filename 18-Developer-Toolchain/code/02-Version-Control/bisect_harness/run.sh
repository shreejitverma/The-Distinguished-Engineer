#!/usr/bin/env bash
set -euo pipefail
# Needs git >= 2.52 (git last-modified); bisect_harness.py checks this and exits with a message.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/bisect_harness.py"
