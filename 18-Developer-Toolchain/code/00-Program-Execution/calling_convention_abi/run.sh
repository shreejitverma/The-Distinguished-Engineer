#!/usr/bin/env bash
set -euo pipefail
# requires: macos

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_BUILD="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_BUILD}"' EXIT

CC="${CC:-cc}"
PYTHON="${PYTHON:-python3}"

"${CC}" -O2 -Wall -Wextra "${SCRIPT_DIR}/harness_native.c" -o "${TMPDIR_BUILD}/harness_native"
"${TMPDIR_BUILD}/harness_native"
"${PYTHON}" "${SCRIPT_DIR}/cross_abi_disasm.py"
