#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CC="${CC:-gcc}"

# Compile deterministic object files with DWARF 4 and DWARF 5 line tables
"${CC}" -gdwarf-4 -O0 -c test_program.c -o test_program_v4.o
"${CC}" -gdwarf-5 -O0 -c test_program.c -o test_program_v5.o

# Execute DWARF line table parser on both; it exits nonzero if any check fails
status=0
python3 parser.py test_program_v4.o test_program_v5.o || status=$?

# Cleanup build artifacts
rm -f test_program_v4.o test_program_v5.o
exit "${status}"
