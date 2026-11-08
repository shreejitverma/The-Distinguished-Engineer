#!/usr/bin/env bash
# requires: linux
#
# Reconstruct a use-after-free from a stripped C++ core dump.
# Builds a release-style binary with a dangling-pointer bug, strips it while
# keeping a build-id debug file aside, crashes it to produce a core, and then
# runs a GDB batch script that recovers the free from the heap bytes the core
# captured. The crash backtrace names the victim, never the culprit; the heap
# forensics name the culprit.
#
# Linux only: the whole drill rests on the kernel's ELF core dump and glibc's
# tcache safe-linking, neither of which exists on macOS. On a non-Linux host the
# harness runs this in the pinned Ubuntu 24.04 container.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cd "${WORK}"

CXX="${CXX:-c++}"

# 1. Build a release-style binary: optimized, with debug info, frame pointers
# kept and sibling-call optimization off so every frame in the backtrace is real
# rather than synthesized, and a stable build id so the split debug file matches.
cp "${SCRIPT_DIR}/uaf.cpp" .
"${CXX}" -O2 -g -fno-omit-frame-pointer -fno-optimize-sibling-calls \
    -fdebug-prefix-map="${WORK}=." -Wl,--build-id \
    -o uaf uaf.cpp

# 2. Split the debug info into a build-id tree and strip the binary, exactly as a
# release pipeline does: the shipped binary has no symbols, the debug file is
# fetched later by build id.
BUILD_ID="$(eu-readelf -n uaf | awk '/Build ID:/ {print $3}')"
objcopy --only-keep-debug uaf uaf.debug
strip --strip-all uaf
DEBUG_ROOT="${WORK}/debug_root"
mkdir -p "${DEBUG_ROOT}/.build-id/${BUILD_ID:0:2}"
cp uaf.debug "${DEBUG_ROOT}/.build-id/${BUILD_ID:0:2}/${BUILD_ID:2}.debug"

# 3. Crash it and capture a core. Prefer the kernel core written by core_pattern
# when it is a plain file name in the working directory; otherwise fall back to a
# GDB-generated core, so the lab runs whatever the host's core_pattern is.
ulimit -c unlimited
PATTERN="$(cat /proc/sys/kernel/core_pattern 2>/dev/null || echo core)"
CORE=""
if [ "${PATTERN%%[|/]*}" = "${PATTERN}" ]; then
    set +e
    ./uaf
    STATUS=$?
    set -e
    # 139 = 128 + SIGSEGV, 135 = 128 + SIGBUS; the wild branch target alignment
    # decides which, and both are the use-after-free faulting.
    if [ "${STATUS}" -ne 139 ] && [ "${STATUS}" -ne 135 ]; then
        echo "expected a memory fault (139 or 135), got ${STATUS}" >&2
        exit 1
    fi
    for c in core "core.${PATTERN}"* core.*; do
        if [ -s "${c}" ]; then CORE="${c}"; break; fi
    done
fi
if [ -z "${CORE}" ]; then
    gdb -q -nx -batch \
        -ex 'run' \
        -ex 'generate-core-file core.gdb' \
        ./uaf >/dev/null 2>&1 || true
    [ -s core.gdb ] && CORE=core.gdb
fi
if [ -z "${CORE}" ]; then
    echo "no core produced" >&2
    exit 1
fi

# 4. Triage the core with the matching debug info. -iex runs before the core is
# read so the build-id debug file is found; the script prints only derived facts.
# GDB's core-load banner carries a varying LWP id and raw addresses, so drop
# everything before the script's first marker line.
gdb -q -nx -batch \
    -iex "set debug-file-directory ${DEBUG_ROOT}" \
    -x "${SCRIPT_DIR}/triage.py" \
    ./uaf "${CORE}" 2>/dev/null \
    | sed -n '/^=== Crash site/,$p'

# 5. Contrast: the same bug under AddressSanitizer is caught at the use, with the
# free and allocation stacks attached, instead of reconstructed from a core. We
# print only deterministic facts: the bug class and which functions ASan names.
echo
echo "=== Contrast: AddressSanitizer on the same source ==="
"${CXX}" -O1 -g -fno-omit-frame-pointer -fsanitize=address -o uaf_asan uaf.cpp
set +e
ASAN_OPTIONS="detect_leaks=0:abort_on_error=0" ./uaf_asan >asan.log 2>&1
set -e
grep -q "ERROR: AddressSanitizer: heap-use-after-free" asan.log \
    && echo "ASan bug class reported: heap-use-after-free" \
    || echo "ASan bug class reported: (none)"
# Each site must appear as a frame inside its own stack section of the report:
# the access stack follows the ERROR line, then "freed by thread", then
# "previously allocated by thread"; a blank line ends a section.
asan_frame() {  # $1 = section start regex, $2 = function name
    awk -v start="$1" -v fn="$2" '
        $0 ~ start { on = 1; next }
        on && /^$/ { on = 0 }
        on && $0 ~ ("^ *#[0-9]+ 0x[0-9a-f]+ in " fn "[( ]") { found = 1 }
        END { exit !found }' asan.log
}
asan_frame '^(READ|WRITE) of size' execute \
    && echo "ASan names the use site (execute): yes" || echo "ASan names the use site (execute): no"
asan_frame '^freed by thread' retire \
    && echo "ASan names the free site (retire): yes" || echo "ASan names the free site (retire): no"
asan_frame '^previously allocated by thread' make_order \
    && echo "ASan names the allocation site (make_order): yes" \
    || echo "ASan names the allocation site (make_order): no"
