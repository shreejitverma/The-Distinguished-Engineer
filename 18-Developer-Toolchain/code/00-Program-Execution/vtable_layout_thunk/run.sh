#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_BUILD="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_BUILD}"' EXIT

CXX_CMD="${CXX:-c++}"
BIN="${TMPDIR_BUILD}/vtable_lab"
OBJ="${TMPDIR_BUILD}/vtable_lab_O0.o"

"${CXX_CMD}" -std=c++17 -Wall -Wextra -O2 -pthread "${SCRIPT_DIR}/main.cpp" -o "${BIN}"
"${BIN}"

# Symbol-level checks. Normalize Mach-O (extra leading underscore) and ELF (version suffix) names.
# The -O0 object keeps every ABI entity (VTTs, construction vtables, thunks) that -O2 inlines away.
"${CXX_CMD}" -std=c++17 -O0 -c "${SCRIPT_DIR}/main.cpp" -o "${OBJ}"

names_of() {
    nm "$1" | awk '{print $NF}' | sed -e 's/@.*$//' -e 's/^__Z/_Z/' -e 's/^___cxa/__cxa/'
}
names_of "${OBJ}" > "${TMPDIR_BUILD}/names_O0.txt"
names_of "${BIN}" > "${TMPDIR_BUILD}/names_O2.txt"

check() {
    local file="$1" label="$2" sym="$3"
    if grep -qxF -- "${sym}" "${TMPDIR_BUILD}/${file}"; then
        echo "[Symbol] ${label}: ${sym}"
    else
        echo "[Symbol] ${label}: ${sym} MISSING"
        exit 1
    fi
}

check names_O0.txt "vtable        " _ZTV12MultiDerived
check names_O0.txt "typeinfo      " _ZTI12MultiDerived
check names_O0.txt "typeinfo name " _ZTS12MultiDerived
check names_O0.txt "adjustor thunk" _ZThn16_N12MultiDerived2f2Ev
check names_O0.txt "VTT           " _ZTT14DiamondDerived
check names_O0.txt "ctor vtable   " _ZTC14DiamondDerived0_9DiamondB1
check names_O0.txt "ctor vtable   " _ZTC14DiamondDerived16_9DiamondB2
check names_O0.txt "virtual thunk " _ZTv0_n40_N14DiamondDerived5vthisEv
check names_O0.txt "covariant thnk" _ZTch0_h16_N10CovDerived3getEv
check names_O2.txt "guard acquire " __cxa_guard_acquire
check names_O2.txt "guard release " __cxa_guard_release
