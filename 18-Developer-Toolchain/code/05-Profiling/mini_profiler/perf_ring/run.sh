#!/usr/bin/env bash
# requires: linux, pmu
# perf_event_open front end of the mini profiler: samples a child through the kernel's perf ring buffer.
# The event is the software cpu-clock, so no hardware counter is involved; "pmu" is declared because the
# harness container's default seccomp profile refuses perf_event_open, and pmu is the tag that keeps
# the lab out of that container. It needs kernel.perf_event_paranoid at 2 or lower and Linux 5.12 or later
# (attr.build_id).
# Shares workload.cpp, symbolize.cpp, image.hpp, and report.py with the parent lab (../).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "${SCRIPT_DIR}")"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

CXX="${CXX:-c++}"
PYTHON="${PYTHON:-python3}"
for tool in "${CXX}" "${PYTHON}"; do
    command -v "${tool}" >/dev/null || { echo "missing required tool: ${tool}" >&2; exit 1; }
done

COMMON=(-std=c++17 -O2 -g -Wall -Wextra)
KEEP_FP=(-fno-omit-frame-pointer -mno-omit-leaf-frame-pointer)

cd "${WORK}"
"${CXX}" "${COMMON[@]}" "${PARENT_DIR}/symbolize.cpp" -o symbolize
"${CXX}" "${COMMON[@]}" "${KEEP_FP[@]}" "${PARENT_DIR}/workload.cpp" "${PARENT_DIR}/sampler.cpp" -Wl,--build-id -o target
"${CXX}" "${COMMON[@]}" "${SCRIPT_DIR}/perf_sampler.cpp" -o perf_sampler

# "-" tells the target not to arm its own SIGPROF sampler: the kernel samples it from outside.
./perf_sampler perf.raw ./target pipeline - 200
./symbolize perf.raw --stats perf.stats > perf.folded

"${PYTHON}" "${PARENT_DIR}/report.py" perf "${WORK}"
