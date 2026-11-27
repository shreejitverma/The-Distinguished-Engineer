#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"
mkdir -p lib_hermetic lib_system

CC="${CC:-cc}"

# Build hermetic library (v202611)
"${CC}" -fPIC -shared -o lib_hermetic/libmath.so "${SCRIPT_DIR}/math_hermetic.c"

# Build outdated system library (v201801)
"${CC}" -fPIC -shared -o lib_system/libmath.so "${SCRIPT_DIR}/math_system.c"

# Which tag does this toolchain emit for a bare -rpath? (distribution default, not upstream ld's)
"${CC}" -o app_default "${SCRIPT_DIR}/main_math.c" -Llib_hermetic -lmath -Wl,-rpath,'$ORIGIN/lib_hermetic'
echo "[DRILL 5] Bare -Wl,-rpath on this toolchain emits: $(readelf -d app_default | grep -oE '\((RPATH|RUNPATH)\)' | paste -sd ' ' -)"

# Scenario A: Binary compiled with DT_RUNPATH (--enable-new-dtags)
echo "[DRILL 5] Compiling app_runpath with DT_RUNPATH (--enable-new-dtags)..."
"${CC}" -o app_runpath "${SCRIPT_DIR}/main_math.c" -Llib_hermetic -lmath -Wl,--enable-new-dtags,-rpath,'$ORIGIN/lib_hermetic'
echo "[DRILL 5] app_runpath dynamic tags: $(readelf -d app_runpath | grep -oE '\((RPATH|RUNPATH)\)' | paste -sd ' ' -)"

echo "[DRILL 5] Case A1: Clean environment (no LD_LIBRARY_PATH):"
./app_runpath

echo "[DRILL 5] Case A2: Tainted environment with LD_LIBRARY_PATH=lib_system:"
if LD_LIBRARY_PATH="$(pwd)/lib_system" ./app_runpath; then
    echo "[DRILL 5] UNEXPECTED: LD_LIBRARY_PATH did not override DT_RUNPATH!"
    exit 1
else
    echo "[DRILL 5] Expected symbol interposition occurred (LD_LIBRARY_PATH took precedence over DT_RUNPATH)."
fi

# Scenario B: Binary compiled with DT_RPATH (--disable-new-dtags)
echo "[DRILL 5] Compiling app_rpath with DT_RPATH (--disable-new-dtags)..."
"${CC}" -o app_rpath "${SCRIPT_DIR}/main_math.c" -Llib_hermetic -lmath -Wl,--disable-new-dtags,-rpath,'$ORIGIN/lib_hermetic'

echo "[DRILL 5] app_rpath dynamic tags: $(readelf -d app_rpath | grep -oE '\((RPATH|RUNPATH)\)' | paste -sd ' ' -)"
echo "[DRILL 5] Case B1: Tainted environment with LD_LIBRARY_PATH=lib_system against DT_RPATH:"
LD_LIBRARY_PATH="$(pwd)/lib_system" ./app_rpath
echo "[DRILL 5] SUCCESS: DT_RPATH immune to LD_LIBRARY_PATH hijacking."
