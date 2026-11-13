#!/usr/bin/env bash
# env_alignment_bias: show how environment size, ASLR, and link order move a benchmark's
# stack data and code relative to cache lines, without timing anything.
# Linux only: it reads the exec stack layout (AT_EXECFN, argv/env strings) that
# fs/exec.c and fs/binfmt_elf.c build, and needs the kernel's sub-page stack randomization.
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
export LC_ALL=C

CC="${CC:-cc}"
CFLAGS=(-O2 -std=c11 -D_GNU_SOURCE -Wall -Wextra -Werror)

cd "${BUILD}"
"${CC}" "${CFLAGS[@]}" -c "${SCRIPT_DIR}/probe.c" -o probe.o
"${CC}" "${CFLAGS[@]}" -c "${SCRIPT_DIR}/pad.c" -o pad.o
"${CC}" "${CFLAGS[@]}" -c "${SCRIPT_DIR}/hot.c" -o hot_default.o
"${CC}" "${CFLAGS[@]}" -falign-functions=64 -c "${SCRIPT_DIR}/hot.c" -o hot_align64.o

"${CC}" probe.o hot_default.o pad.o -o probe
for align in default align64; do
    "${CC}" probe.o pad.o "hot_${align}.o" -o "probe_${align}_pad_first"
    "${CC}" probe.o "hot_${align}.o" pad.o -o "probe_${align}_hot_first"
done

python3 "${SCRIPT_DIR}/driver.py" "${BUILD}"
