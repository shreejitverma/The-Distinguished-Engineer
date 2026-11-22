#!/usr/bin/env bash
# No platform requirements: runs on macOS and Linux as is.
# Drill: diagnose a CI merge queue throughput collapse.
# Pure standard-library Python with seeded random.Random streams; no network, no clock, no paths in the output.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
exec python3 -I "${SCRIPT_DIR}/queue_model.py"
