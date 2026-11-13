#!/usr/bin/env bash
# requires: macos
# Profiles a C++ workload with samply, checks the profile against the workload's own timers,
# then asks llvm-mca what the hot loops should cost and compares that with the measurement.
# Needs (checked below, since the harness `requires:` vocabulary has no token for them):
#   samply    (brew install samply, or cargo install --locked samply)
#   llvm-mca  (brew install llvm; Homebrew keeps it off PATH, so its prefix is searched too)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CXX_BIN="${CXX:-c++}"

die() {
    echo "samply_cpu_profile: $*" >&2
    exit 1
}

command -v samply >/dev/null 2>&1 || die "samply not found on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"

MCA=""
for candidate in "$(command -v llvm-mca 2>/dev/null || true)" \
    /opt/homebrew/opt/llvm/bin/llvm-mca /usr/local/opt/llvm/bin/llvm-mca; do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
        MCA="${candidate}"
        break
    fi
done
[[ -n "${MCA}" ]] || die "llvm-mca not found on PATH or under a Homebrew llvm prefix"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cd "${WORK}"

# The profiling build: optimized like a release, plus what a profiler needs to tell the truth.
#   -g                            line tables and inlined-function records
#   -fno-omit-frame-pointer       a frame record in every function for cheap stack walks
#   -fno-optimize-sibling-calls   keep callers on the stack instead of jumping out of them
"${CXX_BIN}" -std=c++17 -O2 -g -fno-omit-frame-pointer -fno-optimize-sibling-calls \
    "${SCRIPT_DIR}/kernels.cpp" -o kernels
# The same source as a plain optimized build, and as assembly for llvm-mca.
"${CXX_BIN}" -std=c++17 -O2 "${SCRIPT_DIR}/kernels.cpp" -o kernels_plain
"${CXX_BIN}" -std=c++17 -O2 -S "${SCRIPT_DIR}/kernels.cpp" -o kernels.s

record() { # record <profile.json> <command...>: samply's own messages go to stderr
    local profile="$1"
    shift
    samply record --save-only --unstable-presymbolicate --rate 1000 -o "${profile}" -- "$@"
    [[ -s "${profile}" ]] || die "samply wrote no profile for: $*"
}

echo "== 1. Workload under samply, 1000 Hz =="
record full.json ./kernels full.timings
echo
echo "== 2. What the profile says =="
python3 "${SCRIPT_DIR}/analyze_profile.py" full.json --binary kernels --timings full.timings
echo
echo "== 3. What an ordinary -O2 build hides =="
record plain.json ./kernels_plain plain.timings chase >/dev/null
python3 "${SCRIPT_DIR}/analyze_profile.py" plain.json --binary kernels_plain --plain
echo
echo "== 4. What llvm-mca predicts for the hot loops =="
python3 "${SCRIPT_DIR}/mca_predict.py" kernels.s --mca "${MCA}" --timings full.timings
