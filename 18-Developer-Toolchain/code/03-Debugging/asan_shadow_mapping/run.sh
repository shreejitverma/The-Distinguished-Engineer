#!/usr/bin/env bash
# AddressSanitizer and LeakSanitizer internals lab: shadow encoding, redzones, quarantine,
# a rebuilt inline check, report anatomy, and LeakSanitizer reachability classes.
# No requirements line: the lab runs as is on macOS (Apple clang) and on Linux (GCC or clang). Output is derived facts only.
# Build flags follow the clang AddressSanitizer docs; -O0 keeps every planted bug in the binary.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

CXX="${CXX:-c++}"
CXXFLAGS=(-std=c++17 -g -O0 -fno-omit-frame-pointer)
# Only the flags each run states below; nothing inherited from the caller's environment.
unset ASAN_OPTIONS LSAN_OPTIONS
BASE="symbolize=0:detect_leaks=0"

if "${CXX}" --version 2>/dev/null | grep -qi clang; then
    family=clang
else
    family=gcc
fi
echo "compiler family: ${family}"
echo

"${CXX}" "${CXXFLAGS[@]}" -fsanitize=address "${SCRIPT_DIR}/shadow_probe.cpp" -o "${WORK}/probe"
"${CXX}" "${CXXFLAGS[@]}" -fsanitize=address "${SCRIPT_DIR}/bugs.cpp" -o "${WORK}/bugs"
"${CXX}" "${CXXFLAGS[@]}" "${SCRIPT_DIR}/bugs.cpp" -o "${WORK}/bugs_plain"
"${CXX}" "${CXXFLAGS[@]}" -fsanitize=address "${SCRIPT_DIR}/leaks.cpp" -o "${WORK}/leaks_asan"

ASAN_OPTIONS="${BASE}" "${WORK}/probe"
echo "  same loop with ASAN_OPTIONS=quarantine_size_mb=0:"
ASAN_OPTIONS="${BASE}:quarantine_size_mb=0" "${WORK}/probe" | tail -n 1

# run_case LABEL OPTIONS BINARY SCENARIO: run one scenario and summarize its report.
run_case() {
    local label="$1" opts="$2" bin="$3" scenario="$4" status=0
    echo
    echo "-- ${label}"
    # A subshell, so a crash report from bash itself lands in err.txt and not on the terminal.
    (ASAN_OPTIONS="${opts}" "${bin}" "${scenario}"; exit $?) 2>"${WORK}/err.txt" || status=$?
    python3 "${SCRIPT_DIR}/summarize.py" "${WORK}/err.txt" "${status}"
}

echo
echo "== 6. Report anatomy (ASAN_OPTIONS=${BASE}, plus the options shown)"
for s in heap-overflow heap-underflow use-after-free double-free stack-overflow \
    global-overflow use-after-scope container-overflow user-poisoned \
    unaligned-cast unaligned-memcpy; do
    run_case "${s}" "${BASE}" "${WORK}/bugs" "${s}"
done
run_case "use-after-return, default options" "${BASE}" "${WORK}/bugs" use-after-return
run_case "use-after-return, detect_stack_use_after_return=1" \
    "${BASE}:detect_stack_use_after_return=1" "${WORK}/bugs" use-after-return
run_case "stale-write-after-reuse, default quarantine" "${BASE}" "${WORK}/bugs" \
    stale-write-after-reuse
run_case "stale-write-after-reuse, quarantine_size_mb=0" "${BASE}:quarantine_size_mb=0" \
    "${WORK}/bugs" stale-write-after-reuse
run_case "heap-overflow built without -fsanitize=address" "" "${WORK}/bugs_plain" heap-overflow

echo
echo "== 7. LeakSanitizer reachability (sizes are listed in leaks.cpp)"
run_case "ASan with LSan, detect_leaks=1" "symbolize=0:detect_leaks=1" "${WORK}/leaks_asan" ""
echo
echo "-- standalone LSan, -fsanitize=leak"
if "${CXX}" "${CXXFLAGS[@]}" -fsanitize=leak "${SCRIPT_DIR}/leaks.cpp" -o "${WORK}/leaks_lsan" \
    2>/dev/null; then
    status=0
    (LSAN_OPTIONS="symbolize=0" "${WORK}/leaks_lsan"; exit $?) 2>"${WORK}/err.txt" || status=$?
    python3 "${SCRIPT_DIR}/summarize.py" "${WORK}/err.txt" "${status}"
else
    echo "  the compiler driver rejects -fsanitize=leak for this target"
fi
