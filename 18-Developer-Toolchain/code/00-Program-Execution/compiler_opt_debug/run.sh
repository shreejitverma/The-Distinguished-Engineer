#!/usr/bin/env bash
# requires: macos
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

CC="${CC:-cc}"
export LC_ALL=C

"${CC}" -g -O0 -c "${SCRIPT_DIR}/pipeline_demo.c" -o "${TMPDIR}/demo_o0.o"
"${CC}" -g -O2 -c "${SCRIPT_DIR}/pipeline_demo.c" -o "${TMPDIR}/demo_o2.o"

python3 "${SCRIPT_DIR}/analyze.py" "${TMPDIR}/demo_o0.o" "${TMPDIR}/demo_o2.o"
