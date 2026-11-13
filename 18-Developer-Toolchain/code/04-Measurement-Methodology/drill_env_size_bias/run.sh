#!/usr/bin/env bash
# requires: linux, pmu
# Drill: environment size and stack alignment bias (Mytkowicz et al., ASPLOS 2009, Figure 1).
# Builds the paper's micro-kernel, then reproduces the bias, diagnoses it with PMU counters, and removes it.
# Needs perf_event_open(2) on a hardware PMU: kernel.perf_event_paranoid <= 2 or CAP_PERFMON.
# Needs personality(ADDR_NO_RANDOMIZE), which Docker's default seccomp profile refuses.
# DRILL_RAW=1 adds the per-position numbers on stderr.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT
export LC_ALL=C

"${CC:-cc}" -O2 -std=gnu11 -Wall -Wextra -o "${BUILD_DIR}/kernel" "${SCRIPT_DIR}/kernel.c"

if [[ "${DRILL_RAW:-0}" == "1" ]]; then
	python3 "${SCRIPT_DIR}/drill.py" "${BUILD_DIR}/kernel" --raw
else
	python3 "${SCRIPT_DIR}/drill.py" "${BUILD_DIR}/kernel"
fi
