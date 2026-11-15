#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CC="${CC:-gcc}"
BUILD_DIR="${SCRIPT_DIR}/build"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/run_v1" "${BUILD_DIR}/run_v2_broken" "${BUILD_DIR}/run_v2_versioned" "${BUILD_DIR}/run_pimpl"

echo "=== 1. Compile Shared Libraries and Downstream Application ==="
# Build v1 library with SONAME libmarket.so.1
"${CC}" -O2 -g -fPIC -shared -Wl,-soname,libmarket.so.1 -o "${BUILD_DIR}/libmarket_v1.so" v1/market.c
ln -sf "${BUILD_DIR}/libmarket_v1.so" "${BUILD_DIR}/run_v1/libmarket.so.1"
ln -sf "libmarket.so.1" "${BUILD_DIR}/run_v1/libmarket.so"

# Build v2 broken library with IDENTICAL unbumped SONAME libmarket.so.1
"${CC}" -O2 -g -fPIC -shared -Wl,-soname,libmarket.so.1 -o "${BUILD_DIR}/libmarket_v2_broken.so" v2_broken/market.c
ln -sf "${BUILD_DIR}/libmarket_v2_broken.so" "${BUILD_DIR}/run_v2_broken/libmarket.so.1"
ln -sf "libmarket.so.1" "${BUILD_DIR}/run_v2_broken/libmarket.so"

# Build v2 versioned library with symbol versioning map and SONAME libmarket.so.1
"${CC}" -O2 -g -fPIC -shared -Wl,-soname,libmarket.so.1 -Wl,--version-script=v2_versioned/market.map -o "${BUILD_DIR}/libmarket_v2_versioned.so" v2_versioned/market.c
ln -sf "${BUILD_DIR}/libmarket_v2_versioned.so" "${BUILD_DIR}/run_v2_versioned/libmarket.so.1"
ln -sf "libmarket.so.1" "${BUILD_DIR}/run_v2_versioned/libmarket.so"

# Build PImpl library
"${CC}" -O2 -g -fPIC -shared -Wl,-soname,libmarket_pimpl.so.1 -o "${BUILD_DIR}/libmarket_pimpl.so" pimpl/market_pimpl.c
ln -sf "${BUILD_DIR}/libmarket_pimpl.so" "${BUILD_DIR}/run_pimpl/libmarket_pimpl.so.1"
ln -sf "libmarket_pimpl.so.1" "${BUILD_DIR}/run_pimpl/libmarket_pimpl.so"

# Build downstream legacy application compiled against v1 headers
"${CC}" -O2 -g -Iv1 -L"${BUILD_DIR}/run_v1" -o "${BUILD_DIR}/app" app/app.c -lmarket
"${CC}" -O2 -g -Ipimpl -L"${BUILD_DIR}/run_pimpl" -o "${BUILD_DIR}/app_pimpl" app_pimpl/app_pimpl.c -lmarket_pimpl

echo "[build] libmarket.so.1 (v1.0), libmarket.so.1 (v1.1 broken), and app successfully compiled"

echo ""
echo "=== 2. Scenario A: Legacy App running against libmarket v1.0 ==="
LD_LIBRARY_PATH="${BUILD_DIR}/run_v1" "${BUILD_DIR}/app"

echo ""
echo "=== 3. Scenario B: Legacy App running against unbumped libmarket v1.1 (ABI Break) ==="
LD_LIBRARY_PATH="${BUILD_DIR}/run_v2_broken" "${BUILD_DIR}/app"

echo ""
echo "=== 4. Struct Layout Inspection (GDB DWARF Offset Tracing) ==="
echo "[gdb ptype /o v1.0 struct Order]"
gdb -batch -q -ex "file ${BUILD_DIR}/libmarket_v1.so" -ex "ptype /o struct Order" | grep -E "(uint64_t|double|uint32_t|hole|total size)"
echo "[gdb ptype /o v1.1 struct Order]"
gdb -batch -q -ex "file ${BUILD_DIR}/libmarket_v2_broken.so" -ex "ptype /o struct Order" | grep -E "(uint64_t|double|uint32_t|flags|client_timestamp|total size)"

echo ""
echo "=== 5. SONAME and Dynamic Relocation Inspection ==="
echo -n "[readelf soname v1.0] "
readelf -d "${BUILD_DIR}/libmarket_v1.so" | grep SONAME | sed 's/.*\[//;s/\].*//'
echo -n "[readelf soname v1.1] "
readelf -d "${BUILD_DIR}/libmarket_v2_broken.so" | grep SONAME | sed 's/.*\[//;s/\].*//'

echo ""
echo "=== 6. Scenario C: Interface Repair via GNU Symbol Versioning ==="
echo "[readelf version definitions]"
readelf -V "${BUILD_DIR}/libmarket_v2_versioned.so" | grep -E "(Version definition section|MARKET_1.0|MARKET_2.0)" | sed 's/^[ \t]*//'
echo "[execution with versioned shared library]"
LD_LIBRARY_PATH="${BUILD_DIR}/run_v2_versioned" "${BUILD_DIR}/app"

echo ""
echo "=== 7. Scenario D: Interface Repair via Opaque Pointer (PImpl Idiom) ==="
LD_LIBRARY_PATH="${BUILD_DIR}/run_pimpl" "${BUILD_DIR}/app_pimpl"
