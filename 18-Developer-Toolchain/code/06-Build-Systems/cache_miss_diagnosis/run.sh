#!/usr/bin/env bash
# Diagnose Bazel cache misses from compact execution logs.
# requires: network
#
# Network: bazelisk downloads Bazel 9.3.2 on first use, and Bzlmod resolves the
# built-in module graph from the Bazel Central Registry when the repository cache is cold.
#
# Four clean builds of the same workspace, each in its own output base:
#   A  fresh disk cache D1, LAB_FLAVOR=alpha   (populates the cache)
#   B  same disk cache D1, LAB_FLAVOR=alpha    (a "second machine" with the same flags)
#   D  same disk cache D1, LAB_FLAVOR=beta     (a "second machine" with a different env)
#   C  fresh disk cache D2, LAB_FLAVOR=alpha   (re-executes everything)
# A vs B and A vs D explain every cache miss; A vs C finds non-deterministic actions.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export USE_BAZEL_VERSION=9.3.2
export LC_ALL=C

for tool in bazel python3; do
    command -v "${tool}" >/dev/null || { echo "missing required tool: ${tool}" >&2; exit 1; }
done

WORK="$(mktemp -d)"
OUTPUT_BASES=()
cleanup() {
    for ob in "${OUTPUT_BASES[@]+"${OUTPUT_BASES[@]}"}"; do
        (cd "${WORK}/ws" && bazel --ignore_all_rc_files --output_base="${ob}" shutdown) >/dev/null 2>&1 || true
    done
    chmod -R u+w "${WORK}" 2>/dev/null || true
    rm -rf "${WORK}"
}
trap cleanup EXIT

cp -R "${SCRIPT_DIR}/workspace" "${WORK}/ws"
cd "${WORK}/ws"

# build NAME FLAVOR DISK_CACHE: one clean build in its own output base, logging every spawn.
build() {
    local name="$1" flavor="$2" disk_cache="$3"
    local ob="${WORK}/ob_${name}"
    OUTPUT_BASES+=("${ob}")
    if ! LAB_FLAVOR="${flavor}" bazel --ignore_all_rc_files --output_base="${ob}" \
        build //:all \
        --disk_cache="${disk_cache}" \
        --action_env=LAB_FLAVOR \
        --execution_log_compact_file="${WORK}/exec_${name}.log" \
        --lockfile_mode=off --color=no --curses=no --noshow_progress \
        >"${WORK}/build_${name}.out" 2>&1; then
        cat "${WORK}/build_${name}.out" >&2
        exit 1
    fi
    # The INFO process summary goes to stderr only: its strategy names are platform specific.
    grep -E '^INFO: [0-9]+ processes' "${WORK}/build_${name}.out" | sed "s/^/build ${name}: /" >&2 || true
}

build A alpha "${WORK}/disk1"
build B alpha "${WORK}/disk1"
build D beta "${WORK}/disk1"
build C alpha "${WORK}/disk2"

LOG="python3 -I ${SCRIPT_DIR}/execlog.py"

echo "=== Build A: cold cache, LAB_FLAVOR=alpha ==="
${LOG} summary "${WORK}/exec_A.log"
echo
echo "=== Build B: other output base, shared cache, LAB_FLAVOR=alpha ==="
${LOG} summary "${WORK}/exec_B.log"
echo
echo "=== Why B missed: A vs B, matched by first output ==="
${LOG} diff "${WORK}/exec_A.log" "${WORK}/exec_B.log"
echo
echo "=== Disk cache cross-check for B ==="
${LOG} disk-cache "${WORK}/exec_B.log" "${WORK}/disk1"
echo
echo "=== Build D: shared cache, LAB_FLAVOR=beta; why D missed: A vs D ==="
${LOG} summary "${WORK}/exec_D.log"
${LOG} diff "${WORK}/exec_A.log" "${WORK}/exec_D.log"
echo
echo "=== Determinism: A vs C, both executed with a fresh cache ==="
${LOG} determinism "${WORK}/exec_A.log" "${WORK}/exec_C.log"
