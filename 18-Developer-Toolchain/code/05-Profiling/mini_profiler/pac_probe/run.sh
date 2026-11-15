#!/usr/bin/env bash
# requires: macos
# Pointer authentication in frame records: does a saved return address carry a PAC, and does
# XPACLRI (the instruction the mini profiler's walker uses) recover the unsigned address?
# Needs Apple silicon: it builds and runs an arm64e slice and an arm64 slice of one probe.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
CC="${CC:-cc}"
command -v "${CC}" >/dev/null || { echo "missing required tool: ${CC}" >&2; exit 1; }
if [ "$(uname -m)" != "arm64" ]; then
    echo "this lab needs an Apple silicon host (uname -m is $(uname -m))" >&2
    exit 1
fi

FLAGS=(-std=c11 -O1 -Wall -Wextra -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer)
SRC="${SCRIPT_DIR}/pac_probe.c"
cd "${WORK}"

emits() { # emits <instruction> <compiler args...>: does the generated code sign x30 this way?
    local insn="$1"
    shift
    if "${CC}" "${FLAGS[@]}" "$@" -S "${SRC}" -o - | grep -q -w "${insn}"; then echo yes; else echo no; fi
}

report() { # report <label> <probe output>
    local label="$1" line="$2"
    case "${line}" in
        "signed=yes stripped_matches=yes") echo "${label}: saved return address carries a PAC: yes"
                                           echo "${label}: xpaclri recovers the unsigned return address: yes" ;;
        "signed=no stripped_matches=yes")  echo "${label}: saved return address carries a PAC: no"
                                           echo "${label}: xpaclri leaves the unsigned return address unchanged: yes" ;;
        *) echo "${label}: unexpected probe output: ${line}" ;;
    esac
}

echo "== Pointer authentication in AArch64 frame records =="
"${CC}" "${FLAGS[@]}" -arch arm64e "${SRC}" -o probe_arm64e
echo "arm64e: prologue signs x30 with pacibsp: $(emits pacibsp -arch arm64e)"
report "arm64e" "$(./probe_arm64e)"

"${CC}" "${FLAGS[@]}" -arch arm64 "${SRC}" -o probe_arm64
echo "arm64, default flags: prologue signs x30: $(emits paciasp -arch arm64)"
report "arm64, default flags" "$(./probe_arm64)"

"${CC}" "${FLAGS[@]}" -arch arm64 -mbranch-protection=pac-ret "${SRC}" -o probe_pacret
echo "arm64, -mbranch-protection=pac-ret: prologue signs x30 with paciasp: $(emits paciasp -arch arm64 -mbranch-protection=pac-ret)"
report "arm64, -mbranch-protection=pac-ret" "$(./probe_pacret)"
