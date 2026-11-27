#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"
cp "${SCRIPT_DIR}/market_v1.h" "${SCRIPT_DIR}/market_v2_broken.h" .
cp "${SCRIPT_DIR}/libmarket_v1.c" "${SCRIPT_DIR}/libmarket_v2.c" "${SCRIPT_DIR}/app.c" .

CC="${CC:-cc}"

# Step 1: Compile libmarket v1.0 (SONAME libmarket.so.1)
echo "[DRILL 2] Compiling libmarket.so.1 (v1 baseline)..."
"${CC}" -fPIC -shared -Wl,-soname,libmarket.so.1 -o libmarket_v1.so libmarket_v1.c
ln -sf libmarket_v1.so libmarket.so.1
ln -sf libmarket.so.1 libmarket.so

# Step 2: Compile client app against v1 with -fno-stack-protector so client can inspect canary
echo "[DRILL 2] Compiling app against market_v1.h..."
"${CC}" -fno-stack-protector -o app app.c -L. -lmarket -Wl,-rpath,'$ORIGIN'

# Baseline verification
echo "[DRILL 2] Running app against v1 DSO:"
./app

# Step 3: Upgrade libmarket to v2 with unbumped SONAME libmarket.so.1
echo "[DRILL 2] Compiling upgraded libmarket_v2.so (32-byte layout, UNBUMPED SONAME libmarket.so.1)..."
"${CC}" -fPIC -shared -Wl,-soname,libmarket.so.1 -o libmarket_v2.so libmarket_v2.c

# Hot-swap symlink in production environment
ln -sf libmarket_v2.so libmarket.so.1

echo "[DRILL 2] Running unmodified app against silently drifted v2 DSO:"
set +e
./app
APP_EXIT=$?
set -e

if [ "${APP_EXIT}" -ne 0 ]; then
    echo "[DRILL 2] Expected memory corruption detected by client (exit code ${APP_EXIT})."
else
    echo "[DRILL 2] UNEXPECTED: App succeeded despite layout mismatch!"
    exit 1
fi

# Step 4: Remediation - Rebuild with bumped SONAME libmarket.so.2
echo "[DRILL 2] Applying Remediation: Bumping SONAME to libmarket.so.2..."
"${CC}" -fPIC -shared -Wl,-soname,libmarket.so.2 -o libmarket_v2_fixed.so libmarket_v2.c
ln -sf libmarket_v2_fixed.so libmarket.so.2
ln -sf libmarket_v1.so libmarket.so.1
echo "[DRILL 2] Restored libmarket.so.1 for legacy client; app runs cleanly:"
./app
echo "[DRILL 2] SUCCESS: Drill 2 ABI drift isolation and remediation verified."
