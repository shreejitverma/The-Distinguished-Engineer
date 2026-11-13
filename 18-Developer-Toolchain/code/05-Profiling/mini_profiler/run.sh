#!/usr/bin/env bash
# Mini sampling profiler: SIGPROF sampler -> raw addresses -> ELF/Mach-O symbolizer -> folded stacks.
# Runs on macOS and Linux as is. The perf_event_open ring-buffer front end is the separate lab in perf_ring/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

CXX="${CXX:-c++}"
PYTHON="${PYTHON:-python3}"
for tool in "${CXX}" "${PYTHON}" nm strip; do
    command -v "${tool}" >/dev/null || { echo "missing required tool: ${tool}" >&2; exit 1; }
done

# -fno-omit-frame-pointer keeps a frame record in every function that makes a call.
# -mno-omit-leaf-frame-pointer extends that to leaf functions; the leafless build drops it again.
COMMON=(-std=c++17 -O2 -g -Wall -Wextra)
KEEP_FP=(-fno-omit-frame-pointer -mno-omit-leaf-frame-pointer)
LEAFLESS=(-fno-omit-frame-pointer -momit-leaf-frame-pointer)
LINK=()
if [ "$(uname -s)" = "Linux" ]; then
    LINK=(-Wl,--build-id) # Mach-O gets its LC_UUID from the linker without asking
fi
SOURCES=("${SCRIPT_DIR}/workload.cpp" "${SCRIPT_DIR}/sampler.cpp")

cd "${WORK}"
"${CXX}" "${COMMON[@]}" "${SCRIPT_DIR}/symbolize.cpp" -o symbolize
"${CXX}" "${COMMON[@]}" "${KEEP_FP[@]}" "${SOURCES[@]}" ${LINK[@]+"${LINK[@]}"} -o target
"${CXX}" "${COMMON[@]}" "${LEAFLESS[@]}" "${SOURCES[@]}" ${LINK[@]+"${LINK[@]}"} -o target_leafless
"${CXX}" "${COMMON[@]}" "${KEEP_FP[@]}" -DMINIPROF_OTHER_BUILD "${SOURCES[@]}" ${LINK[@]+"${LINK[@]}"} -o target_other

# 1. Sample. Each run profiles itself until it holds the requested number of samples.
echo 1200 > requested
./target pipeline pipeline.raw 1200
./target pitfalls pitfalls.raw 600
./target_leafless pipeline leafless.raw 600

# 2. Symbolize and fold. The raw files hold addresses only; names come from the files on disk.
./symbolize pipeline.raw --stats pipeline.stats > pipeline.folded
./symbolize pitfalls.raw > pitfalls.folded
./symbolize leafless.raw > leafless.folded
./symbolize --symbols target > target.symbols

# 3. Symbolize the same samples against damaged or wrong copies of the executable.
cp target target_strip_x
strip -x target_strip_x 2>/dev/null # local symbols removed, global ones kept
cp target target_stripped
strip target_stripped 2>/dev/null   # every symbol the linker does not need at run time removed
./symbolize pipeline.raw --exe target_strip_x --stats strip_x.stats > strip_x_bounded.folded
./symbolize pipeline.raw --exe target_strip_x --lookup nearest > strip_x_nearest.folded
./symbolize pipeline.raw --exe target_stripped --stats stripped.stats > stripped.folded
set +e
./symbolize pipeline.raw --exe target_other > other.folded 2>/dev/null
echo "$?" > other.status
set -e

"${PYTHON}" "${SCRIPT_DIR}/report.py" main "${WORK}"
