#!/usr/bin/env bash
# requires: linux, pmu
#
# Stage 1 drives perf_event_open(2) and its mmap ring buffer directly with a
# software clock event. Stage 2 records a false-sharing program and its padded
# fix with `perf c2c record` and derives the diagnosis from the samples.
#
# Needs: perf with memory sampling (Intel PEBS load latency, AMD IBS op, which
# perf-c2c(1) says excludes Zen 3, or Arm SPE), python3, a C compiler, and either
# CAP_PERFMON or kernel.perf_event_paranoid <= 1, because perf c2c records
# physical addresses.
# KEEP_WORKDIR=1 keeps the recordings and the full perf c2c reports.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
cleanup() {
    if [ "${KEEP_WORKDIR:-0}" = "1" ]; then
        echo "work directory kept: ${WORK}" >&2
    else
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT

die() {
    echo "run.sh: $*" >&2
    exit 1
}

[ "$(uname -s)" = "Linux" ] || die "this lab needs a Linux kernel"
command -v perf >/dev/null || die "perf is not on PATH (linux-perf, linux-tools, or perf package)"
command -v python3 >/dev/null || die "python3 is not on PATH"

export LC_ALL=C
# Keep perf away from the user's ~/.perfconfig and ~/.debug build-id cache.
export HOME="${WORK}/home"
export PERF_CONFIG=/dev/null
mkdir -p "${HOME}"

LINE_FILE=/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size
[ -r "${LINE_FILE}" ] || die "cannot read ${LINE_FILE}"
LINE="$(cat "${LINE_FILE}")"

CC_BIN="${CC:-cc}"
CFLAGS=(-O2 -g -Wall -Wextra -pthread)
"${CC_BIN}" "${CFLAGS[@]}" -o "${WORK}/ring_buffer" "${SCRIPT_DIR}/ring_buffer.c"
"${CC_BIN}" "${CFLAGS[@]}" "-DLINE=${LINE}" -DPADDED=0 -o "${WORK}/fs_packed" "${SCRIPT_DIR}/false_sharing.c"
"${CC_BIN}" "${CFLAGS[@]}" "-DLINE=${LINE}" -DPADDED=1 -o "${WORK}/fs_padded" "${SCRIPT_DIR}/false_sharing.c"

echo "== Stage 1: perf_event_open and the mmap ring buffer =="
"${WORK}/ring_buffer" || die "ring_buffer failed; check kernel.perf_event_paranoid (2 or lower is enough here)"

read -r CPU_A CPU_B < <(python3 "${SCRIPT_DIR}/c2c_check.py" pick-cpus) ||
    die "could not pick two CPUs on different physical cores"
[ -n "${CPU_A:-}" ] && [ -n "${CPU_B:-}" ] || die "could not pick two CPUs on different physical cores"

DURATION_MS=3000

# record <variant>: perf c2c record, then the two views the checker reads.
record() {
    local variant="$1"
    local data="${WORK}/${variant}.data"

    # Options before `--` belong to perf c2c record, options after it to perf record.
    if ! perf c2c record --all-user -- -q -N -o "${data}" \
        "${WORK}/fs_${variant}" "${WORK}/${variant}.info" "${CPU_A}" "${CPU_B}" "${DURATION_MS}" \
        >"${WORK}/${variant}.record.log" 2>&1; then
        tail -n 20 "${WORK}/${variant}.record.log" >&2
        die "perf c2c record failed: it needs memory sampling hardware and CAP_PERFMON or kernel.perf_event_paranoid <= 1"
    fi

    if ! perf --no-pager script -i "${data}" -F addr,data_src \
        >"${WORK}/${variant}.script" 2>"${WORK}/${variant}.script.log"; then
        tail -n 20 "${WORK}/${variant}.script.log" >&2
        die "perf script failed on ${variant}.data"
    fi

    if ! perf --no-pager c2c report -i "${data}" --stdio \
        >"${WORK}/${variant}.c2c" 2>"${WORK}/${variant}.c2c.log"; then
        tail -n 20 "${WORK}/${variant}.c2c.log" >&2
        die "perf c2c report failed on ${variant}.data"
    fi
}

record packed
record padded

# The same programs again without perf, so the throughput comparison carries no
# sampling overhead.
"${WORK}/fs_packed" "${WORK}/packed_free.info" "${CPU_A}" "${CPU_B}" 1000
"${WORK}/fs_padded" "${WORK}/padded_free.info" "${CPU_A}" "${CPU_B}" 1000

echo
echo "== Stage 2: perf c2c on a false-sharing pair and its padded fix =="
python3 "${SCRIPT_DIR}/c2c_check.py" report "${WORK}"
