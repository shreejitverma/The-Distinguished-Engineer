#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_BUILD="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_BUILD}"' EXIT
cd "${TMPDIR_BUILD}"

CC="${CC:-gcc}"
CXX="${CXX:-g++}"
PYTHON="${PYTHON:-python3}"

echo "=== 1. Frame Pointer vs DWARF CFI Unwinding ==="
echo "[*] Compiling with -O2 -fomit-frame-pointer..."
"${CC}" -O2 -fomit-frame-pointer -rdynamic "${SCRIPT_DIR}/cfi_unwind.c" -o unwind_omit
./unwind_omit

echo ""
echo "[*] Compiling with -O2 -fno-omit-frame-pointer..."
"${CC}" -O2 -fno-omit-frame-pointer -rdynamic "${SCRIPT_DIR}/cfi_unwind.c" -o unwind_no_omit
./unwind_no_omit

echo ""
echo "=== 2. Inspecting .eh_frame_hdr Binary Search Index ==="
"${PYTHON}" "${SCRIPT_DIR}/parse_eh_frame_hdr.py" unwind_no_omit

echo ""
echo "=== 3. Itanium Two-Phase Exception Handling Verification ==="
"${CXX}" -O2 "${SCRIPT_DIR}/two_phase.cpp" -Wl,--wrap=__gxx_personality_v0 -o two_phase
echo "[*] Scenario A: Handled exception (Phase 1 finds catch block -> Phase 2 runs cleanups)"
./two_phase

echo ""
echo "[*] Scenario B: Unhandled exception (Phase 1 finds no catch block -> Phase 2 never starts)"
./two_phase --uncaught
