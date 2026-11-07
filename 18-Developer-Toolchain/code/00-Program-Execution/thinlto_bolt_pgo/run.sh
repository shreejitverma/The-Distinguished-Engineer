#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

CC="${CC:-gcc}"
PYTHON="${PYTHON:-python3}"

echo "=== 1. Baseline Compilation (No LTO, No PGO) ==="
"${CC}" -O2 -c "${SCRIPT_DIR}/packet_engine.c" -o "${BUILD_DIR}/packet_engine.o"
"${CC}" -O2 -c "${SCRIPT_DIR}/main.c" -o "${BUILD_DIR}/main.o"
"${CC}" -O2 "${BUILD_DIR}/packet_engine.o" "${BUILD_DIR}/main.o" -o "${BUILD_DIR}/app_baseline"
"${BUILD_DIR}/app_baseline"
# Same objects, linker section GC on: without -ffunction-sections all functions share one .text section, so nothing can be dropped.
"${CC}" -O2 -Wl,--gc-sections "${BUILD_DIR}/packet_engine.o" "${BUILD_DIR}/main.o" -o "${BUILD_DIR}/app_baseline_gc"

echo "=== 2. Link-Time Optimization (LTO / Whole Program Optimization) ==="
"${CC}" -O2 -flto -c "${SCRIPT_DIR}/packet_engine.c" -o "${BUILD_DIR}/packet_engine_lto.o"
"${CC}" -O2 -flto -c "${SCRIPT_DIR}/main.c" -o "${BUILD_DIR}/main_lto.o"
"${CC}" -O2 -flto "${BUILD_DIR}/packet_engine_lto.o" "${BUILD_DIR}/main_lto.o" -o "${BUILD_DIR}/app_lto"
"${BUILD_DIR}/app_lto"

echo "=== 3. Profile-Guided Optimization (PGO Instrumentation & Feedback) ==="
mkdir -p "${BUILD_DIR}/prof_data"
"${CC}" -O2 -flto -fprofile-generate="${BUILD_DIR}/prof_data" -c "${SCRIPT_DIR}/packet_engine.c" -o "${BUILD_DIR}/packet_engine.o"
"${CC}" -O2 -flto -fprofile-generate="${BUILD_DIR}/prof_data" -c "${SCRIPT_DIR}/main.c" -o "${BUILD_DIR}/main.o"
"${CC}" -O2 -flto -fprofile-generate="${BUILD_DIR}/prof_data" "${BUILD_DIR}/packet_engine.o" "${BUILD_DIR}/main.o" -o "${BUILD_DIR}/app_gen"
"${BUILD_DIR}/app_gen" > /dev/null

# -Werror=missing-profile turns "profile data file not found" into a build failure instead of a silent non-PGO build.
"${CC}" -O2 -flto -fprofile-use="${BUILD_DIR}/prof_data" -Werror=missing-profile -c "${SCRIPT_DIR}/packet_engine.c" -o "${BUILD_DIR}/packet_engine.o"
"${CC}" -O2 -flto -fprofile-use="${BUILD_DIR}/prof_data" -Werror=missing-profile -c "${SCRIPT_DIR}/main.c" -o "${BUILD_DIR}/main.o"
"${CC}" -O2 -flto -fprofile-use="${BUILD_DIR}/prof_data" -Werror=missing-profile "${BUILD_DIR}/packet_engine.o" "${BUILD_DIR}/main.o" -o "${BUILD_DIR}/app_pgo"
"${BUILD_DIR}/app_pgo"

echo "=== 4. Layout & Optimization Analysis ==="
"${PYTHON}" "${SCRIPT_DIR}/analyze_layout.py" "${BUILD_DIR}/app_baseline" "${BUILD_DIR}/app_baseline_gc" "${BUILD_DIR}/app_lto" "${BUILD_DIR}/app_pgo"
