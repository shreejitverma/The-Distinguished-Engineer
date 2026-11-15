#!/usr/bin/env bash
# C++20 named modules through Ninja dyndep: scan (P1689R5) -> collate -> ordered compiles,
# then the same sources through CMake's native cxxmodules support.
# Needs: a Clang 16+ C++ compiler with clang-scan-deps from the same toolchain, ninja 1.11+,
# cmake 3.28+, python3. Apple's Xcode clang ships no clang-scan-deps, so when CXX is unset and
# `c++` has no sibling scanner, the lab uses the LLVM toolchain that provides clang-scan-deps.
# No harness requirement applies: it runs on macOS as is once Homebrew llvm, ninja, and cmake exist.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

resolve() { command -v "$1" 2>/dev/null || true; }

CXX_IN="${CXX:-c++}"
CXX_PATH="$(resolve "${CXX_IN}")"
SCAN="${CLANG_SCAN_DEPS:-}"
if [[ -z "${SCAN}" && -n "${CXX_PATH}" && -x "$(dirname "${CXX_PATH}")/clang-scan-deps" ]]; then
  SCAN="$(dirname "${CXX_PATH}")/clang-scan-deps"
fi
if [[ -z "${SCAN}" && -z "${CXX:-}" ]]; then
  for cand in "$(resolve clang-scan-deps)" /opt/homebrew/opt/llvm/bin/clang-scan-deps /usr/local/opt/llvm/bin/clang-scan-deps; do
    if [[ -n "${cand}" && -x "${cand}" && -x "$(dirname "${cand}")/clang++" ]]; then
      SCAN="${cand}"
      CXX_PATH="$(dirname "${cand}")/clang++"
      break
    fi
  done
fi
if [[ -z "${SCAN}" || -z "${CXX_PATH}" ]]; then
  echo "error: need a clang++ with clang-scan-deps beside it (set CXX and CLANG_SCAN_DEPS)" >&2
  exit 1
fi
NINJA="$(resolve ninja)"
CMAKE="$(resolve cmake)"
[[ -n "${NINJA}" && -n "${CMAKE}" ]] || { echo "error: need ninja and cmake on PATH" >&2; exit 1; }

# Optional: GCC 14+ reproduces ninja issue 2666 (its depfiles list the CMIs a compile read).
# Without one, section 10 prints a skip line instead, so the output differs on such hosts.
GXX_PATH="${GXX:-}"
if [[ -z "${GXX_PATH}" ]]; then
  for cand in g++-16 g++-15 g++-14; do
    p="$(resolve "${cand}")"
    if [[ -n "${p}" ]] && "${p}" --version 2>/dev/null | head -n 1 | grep -q GCC; then GXX_PATH="${p}"; break; fi
  done
fi

major() { "$1" --version | sed -n 's/.*version \([0-9][0-9]*\)\..*/\1/p' | head -n 1; }
gcc_major() { "$1" -dumpversion | cut -d. -f1; }
echo "toolchain: clang $(major "${CXX_PATH}"), clang-scan-deps $(major "${SCAN}"), ninja $("${NINJA}" --version), cmake $("${CMAKE}" --version | sed -n 's/cmake version //p')${GXX_PATH:+, gcc $(gcc_major "${GXX_PATH}")}"

CXX="${CXX_PATH}" SCAN_DEPS="${SCAN}" NINJA="${NINJA}" CMAKE="${CMAKE}" GXX="${GXX_PATH}" LAB_DIR="${LAB_DIR}" \
  python3 "${LAB_DIR}/lab.py" "${WORK}"
