#!/usr/bin/env bash
# C++ heap profiling: heaptrack, Massif, DHAT, and allocator telemetry on one workload.
# requires: linux
#
# Needs heaptrack (heaptrack and heaptrack_print), valgrind (Massif and DHAT),
# libjemalloc.so.2, a C++17 compiler, and python3. In the harness container these
# come from the heaptrack, valgrind, and libjemalloc2 packages in
# code/_harness/linux-labs.Dockerfile. A missing tool is a failure, never a skip.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
# Allocator settings inherited from the caller would change the telemetry lines.
unset MALLOC_CONF LD_PRELOAD GLIBC_TUNABLES MALLOC_ARENA_MAX MALLOC_ARENA_TEST MALLOC_TOP_PAD_ \
    MALLOC_PERTURB_ MALLOC_MMAP_THRESHOLD_ MALLOC_TRIM_THRESHOLD_ MALLOC_MMAP_MAX_ MIMALLOC_SHOW_STATS

for tool in heaptrack heaptrack_print valgrind python3; do
    if ! command -v "${tool}" >/dev/null; then
        echo "${tool} not found on PATH" >&2
        exit 1
    fi
done
# awk reads all input (no early exit), so ldconfig never dies of SIGPIPE under
# pipefail; "|| true" keeps a missing or failing ldconfig from aborting the script.
JEMALLOC="$( (ldconfig -p 2>/dev/null || true) | awk '/libjemalloc\.so\.2 / && !found {print $NF; found = 1}')"
if [ -z "${JEMALLOC}" ]; then
    for candidate in /usr/lib/*/libjemalloc.so.2 /usr/lib/libjemalloc.so.2 /usr/local/lib/libjemalloc.so.2; do
        if [ -e "${candidate}" ]; then JEMALLOC="${candidate}"; break; fi
    done
fi
if [ -z "${JEMALLOC}" ]; then
    echo "libjemalloc.so.2 not found (install libjemalloc2)" >&2
    exit 1
fi

# -O0 keeps every allocation and every frame; -g gives the tools file:line.
"${CXX:-c++}" -std=c++17 -O0 -g -fno-omit-frame-pointer \
    -o "${WORK}/workload" "${SCRIPT_DIR}/workload.cpp"
cd "${WORK}"

# quiet LOG CMD... : run CMD with all output in LOG; on failure show the log and stop.
quiet() {
    local log="$1"
    shift
    if ! "$@" >"${log}" 2>&1; then
        echo "command failed: $*" >&2
        tail -n 40 "${log}" >&2
        exit 1
    fi
}

digest() { python3 -I "${SCRIPT_DIR}/digest.py" "$@"; }

N=100000

echo "== heaptrack: every malloc and free, with a backtrace =="
for mode in none churn reuse pmr peakleak; do
    quiet "ht_${mode}.log" heaptrack --record-only -o "${WORK}/ht_${mode}" ./workload "${mode}" "${N}"
    # heaptrack compresses with zstd when its build and PATH have it, else gzip.
    data=""
    for ext in zst gz; do
        if [ -e "ht_${mode}.${ext}" ]; then data="ht_${mode}.${ext}"; fi
    done
    if [ -z "${data}" ]; then
        echo "heaptrack wrote no data file for ${mode}" >&2
        exit 1
    fi
    # --print-leaks adds the MEMORY LEAKS section; the other sections are on by default.
    # The "=true" form keeps boost::program_options from reading the file name as its value.
    quiet "ht_${mode}.txt" heaptrack_print --print-leaks=true -f "${data}"
done
digest heaptrack "${N}" ht_none.txt ht_churn.txt ht_reuse.txt ht_pmr.txt ht_peakleak.txt
echo

echo "== Massif: heap snapshots over time =="
quiet ms_peak.log valgrind --tool=massif --massif-out-file="${WORK}/ms_peak.out" ./workload peak
quiet ms_half.log valgrind --tool=massif --massif-out-file="${WORK}/ms_half.out" ./workload small 50000
quiet ms_full.log valgrind --tool=massif --massif-out-file="${WORK}/ms_full.out" ./workload small 100000
digest massif ms_peak.out ms_half.out ms_full.out 50000 100000
echo

echo "== DHAT: lifetimes and access counts per allocation point =="
quiet dhat.log valgrind --tool=dhat --num-callers=24 --dhat-out-file="${WORK}/dhat.json" ./workload dhat 10000
digest dhat dhat.json
echo

echo "== Allocator telemetry: glibc malloc (mallinfo2, malloc_trim) =="
./workload telemetry
echo

echo "== Allocator telemetry: jemalloc defaults (LD_PRELOAD, mallctl) =="
LD_PRELOAD="${JEMALLOC}" ./workload telemetry
echo

echo "== Allocator telemetry: jemalloc with MALLOC_CONF=dirty_decay_ms:0 =="
LD_PRELOAD="${JEMALLOC}" MALLOC_CONF="dirty_decay_ms:0" ./workload telemetry
