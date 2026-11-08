#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_BUILD="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_BUILD}"' EXIT
# Compile from the lab directory so the line table records the bare name test_program.c.
cd "${SCRIPT_DIR}"

CC="${CC:-gcc}"

# Compile deterministic object files with DWARF 4 and DWARF 5 line tables
"${CC}" -gdwarf-4 -O0 -c test_program.c -o "${TMPDIR_BUILD}/test_program_v4.o"
"${CC}" -gdwarf-5 -O0 -c test_program.c -o "${TMPDIR_BUILD}/test_program_v5.o"

# Execute DWARF line table parser on both; it exits nonzero if any check fails
python3 parser.py "${TMPDIR_BUILD}/test_program_v4.o" "${TMPDIR_BUILD}/test_program_v5.o"
