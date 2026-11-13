#!/usr/bin/env bash
# Hardware performance counters, Top-Down TMA level 1, and Roofline bounds.
# requires: linux, pmu
#
# Part 1 and 2 are pure arithmetic over published formulas (any OS).
# Part 3 opens real counters with perf_event_open(2) and needs a Linux host whose
# PMU is visible (bare metal, or a VM with a virtual PMU) and
# kernel.perf_event_paranoid <= 2. It prints derived facts, never raw counts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

echo "== part 1: TMA level 1 from counter values (tma_model.py)"
python3 "${SCRIPT_DIR}/tma_model.py"
echo
echo "== part 2: Roofline bounds (roofline.py)"
python3 "${SCRIPT_DIR}/roofline.py"
echo
echo "== part 3: live counters (pmc_lab.c)"
"${CC:-cc}" -O2 -std=gnu11 -Wall -Wextra -Werror -o "${WORK}/pmc_lab" "${SCRIPT_DIR}/pmc_lab.c"
"${WORK}/pmc_lab"
