#!/usr/bin/env bash
# ThreadSanitizer happens-before lab, plus a MemorySanitizer stage where the compiler supports it.
# Needs a C++17 compiler with -fsanitize=thread (Apple clang, clang, or g++ with libtsan) and python3.
# No harness requirement line: the lab runs on macOS and Linux hosts as is.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
CXX="${CXX:-c++}"
export LC_ALL=C

cd "${SCRIPT_DIR}"

# Stage 1 input: assembly of the instrumented unit, so the inserted __tsan_* calls are visible.
"${CXX}" -std=c++17 -O1 -fsanitize=thread -S instr.cpp -o "${BUILD}/instr.s"

# Stage 2 and 3 input: the race scenarios, built the way the Clang docs recommend (-O1 -g).
"${CXX}" -std=c++17 -O1 -g -fsanitize=thread races.cpp -o "${BUILD}/races" -lpthread

# Stage 4 input: MSan builds, only when this compiler accepts -fsanitize=memory for this target.
if "${CXX}" -fsanitize=memory -x c++ -c /dev/null -o "${BUILD}/probe.o" 2>"${BUILD}/probe.err"; then
    echo accepted >"${BUILD}/msan_probe.txt"
    "${CXX}" -std=c++17 -O1 -g -fsanitize=memory -fsanitize-memory-track-origins=2 \
        msan_demo.cpp -o "${BUILD}/msan_eager"
    "${CXX}" -std=c++17 -O1 -g -fsanitize=memory -fsanitize-memory-track-origins=2 \
        -fno-sanitize-memory-param-retval msan_demo.cpp -o "${BUILD}/msan_lazy"
else
    # Keep the diagnostic but drop the OS version from the target triple.
    sed -n '1s/darwin[0-9.]*/darwin/;1p' "${BUILD}/probe.err" | sed 's/^[^:]*: error: //' >"${BUILD}/msan_probe.txt"
fi

python3 "${SCRIPT_DIR}/driver.py" "${BUILD}"
