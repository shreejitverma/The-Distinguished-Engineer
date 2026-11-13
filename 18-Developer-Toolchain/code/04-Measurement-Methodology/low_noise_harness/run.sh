#!/usr/bin/env bash
# requires: linux
#
# Low-noise benchmark harness lab: preflight gate, interleaved A/B schedule,
# Mann-Whitney U test, and a pinned live run. Deterministic facts go to stdout;
# timings and host-specific details go to stderr.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

"${CC:-cc}" -O2 -Wall -Wextra -o "${WORK}/workload" "${SCRIPT_DIR}/workload.c"
python3 "${SCRIPT_DIR}/lab.py" "${WORK}"
