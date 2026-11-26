#!/usr/bin/env bash
# Flake economics lab: closed forms checked against seeded Monte Carlo, a merge-queue
# bisection simulation, rerun-based flake detection, an SPRT quarantine rule, and a toy
# predictive test selector. Standard library only; every random draw comes from a seeded
# random.Random, so the output is identical on every run and platform.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
exec python3 -I "${SCRIPT_DIR}/flake_lab.py"
