#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"
mkdir -p staging

CXX="${CXX:-g++}"

# Phase 1: Build liborder.so.1 (SONAME: liborder.so.1)
${CXX} -O2 -fPIC -shared "${SCRIPT_DIR}/v1/order.cpp" -I"${SCRIPT_DIR}" \
    -Wl,-soname,liborder.so.1 \
    -Wl,--version-script="${SCRIPT_DIR}/v1/order.map" \
    -o staging/liborder.so.1.0.0

ln -s liborder.so.1.0.0 staging/liborder.so.1
ln -s liborder.so.1 staging/liborder.so

# Phase 2: Build consumer_v1 against v1
${CXX} -O2 "${SCRIPT_DIR}/consumer_v1.cpp" -I"${SCRIPT_DIR}" \
    -Lstaging -lorder -Wl,-rpath,'$ORIGIN/staging' \
    -o consumer_v1

# Phase 3: Build liborder.so.2 (SONAME: liborder.so.2)
${CXX} -O2 -fPIC -shared "${SCRIPT_DIR}/v2/order.cpp" -I"${SCRIPT_DIR}" \
    -Wl,-soname,liborder.so.2 \
    -Wl,--version-script="${SCRIPT_DIR}/v2/order.map" \
    -o staging/liborder.so.2.0.0

ln -s liborder.so.2.0.0 staging/liborder.so.2
# Update development symlink to point to v2
rm -f staging/liborder.so
ln -s liborder.so.2 staging/liborder.so

# Phase 4: Build consumer_v2 against v2
${CXX} -O2 "${SCRIPT_DIR}/consumer_v2.cpp" -I"${SCRIPT_DIR}" \
    -Lstaging -lorder -Wl,-rpath,'$ORIGIN/staging' \
    -o consumer_v2

# Phase 5: Verification and Deterministic Output
echo "=== Phase 1: Binary Dependency Check ==="
readelf -d consumer_v1 | grep NEEDED | grep liborder | sed 's/.*\[\(.*\)\].*/consumer_v1 NEEDED: \1/'
readelf -d consumer_v2 | grep NEEDED | grep liborder | sed 's/.*\[\(.*\)\].*/consumer_v2 NEEDED: \1/'

echo "=== Phase 2: SONAME Tag Check ==="
readelf -d staging/liborder.so.1.0.0 | grep SONAME | sed 's/.*\[\(.*\)\].*/liborder.so.1 SONAME: \1/'
readelf -d staging/liborder.so.2.0.0 | grep SONAME | sed 's/.*\[\(.*\)\].*/liborder.so.2 SONAME: \1/'

echo "=== Phase 3: Side-by-Side Coexistence Execution ==="
./consumer_v1
./consumer_v2

echo "=== Phase 4: Symbol Mangling and Version Node Check ==="
readelf --dyn-syms --wide staging/liborder.so.1.0.0 | grep 'process' | awk '{print $8}' | sed 's/@.*//'
readelf --dyn-syms --wide staging/liborder.so.2.0.0 | grep 'process' | awk '{print $8}' | sed 's/@.*//'

echo "=== Phase 5: Filesystem Co-installation Layout ==="
ls -1 staging | sort
