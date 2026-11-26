#!/usr/bin/env bash
# Runs on macOS and Linux as is; no network, no extra tools.
# Quota-aware routing, retry herding, supervisor restart intensity, review-queue
# saturation, and verifier independence, all as seeded deterministic models.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
python3 -I "${SCRIPT_DIR}/quota_router.py"
