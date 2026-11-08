#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
cd "${WORK_DIR}"

CC="${CC:-gcc}"

# 1. libdep2.so: a leaf shared library
"${CC}" -fPIC -shared -O2 -Wall -Wextra \
    -Wl,-soname,libdep2.so \
    -o libdep2.so "${SCRIPT_DIR}/libdep2.c"

# 2. libdep.so: depends on libdep2.so (DT_NEEDED), so libdep2.so must initialize first
"${CC}" -fPIC -shared -O2 -Wall -Wextra \
    -Wl,-soname,libdep.so \
    -Wl,-rpath,'$ORIGIN' \
    -o libdep.so "${SCRIPT_DIR}/libdep.c" \
    -L. -ldep2

# 3. main executable: depends on libdep.so, which depends on libdep2.so
"${CC}" -O2 -Wall -Wextra -pthread \
    "${SCRIPT_DIR}/main.c" \
    -L. -ldep \
    -Wl,-rpath,'$ORIGIN' \
    -Wl,-rpath-link,. \
    -o main

# 4. Execute the binary and display the initialization and termination lifecycle
./main
