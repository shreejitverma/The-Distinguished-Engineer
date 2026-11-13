#!/usr/bin/env bash
# requires: linux, pmu
#
# Drill: three cache-line contention bugs in one ingest loop, diagnosed with perf c2c.
# Needs perf with memory sampling (Intel PEBS, AMD IBS on Zen 4 or later, or Arm SPE), at least
# two physical cores, and kernel.perf_event_paranoid <= 1 or CAP_PERFMON (perf c2c record adds
# --phys-data). stdout carries pass facts only; the measured numbers go to stderr.
#
# FS_DRILL_STAGES=selftest,layout,throughput  runs the stages that need no PMU (not a recording).
# FS_DRILL_KEEP=<dir>                         keeps the binary, perf.data files, and reports.
set -euo pipefail
export LC_ALL=C

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# The unit that moves between cores. sysconf reports the L1 line; the coherency granule in sysfs
# can be larger (128 on Apple silicon), and aligning to the smaller one leaves the bug in place.
line="$(getconf LEVEL1_DCACHE_LINESIZE 2>/dev/null || echo 0)"
case "${line}" in '' | *[!0-9]*) line=0 ;; esac
for f in /sys/devices/system/cpu/cpu0/cache/index*/coherency_line_size; do
    [[ -r "${f}" ]] || continue
    n="$(cat "${f}")"
    if [[ "${n}" =~ ^[0-9]+$ ]] && ((n > line)); then line="${n}"; fi
done
((line >= 64)) || line=64
if ((line & (line - 1))); then
    echo "drill_false_sharing: FAIL: cache line size ${line} is not a power of two" >&2
    exit 1
fi

"${CC:-cc}" -O2 -g -Wall -Wextra -Werror -pthread -DFS_LINE="${line}" "${here}/ingest.c" -o "${work}/ingest"

status=0
python3 "${here}/analyze.py" --binary "${work}/ingest" --fixtures "${here}/fixtures" \
    --work "${work}" --line "${line}" || status=$?

if [[ -n "${FS_DRILL_KEEP:-}" ]]; then
    mkdir -p "${FS_DRILL_KEEP}"
    cp -R "${work}/." "${FS_DRILL_KEEP}/"
fi
exit "${status}"
