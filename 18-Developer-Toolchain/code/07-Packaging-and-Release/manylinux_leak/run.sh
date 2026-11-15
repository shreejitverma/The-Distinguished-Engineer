#!/usr/bin/env bash
# requires: docker
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

cp -R "${SCRIPT_DIR}/." "${TMP_DIR}/"
cd "${TMP_DIR}"

BUILD_IMAGE="toolchain-labs:1192e7ee37dc"
DEPLOY_IMAGE="ubuntu:22.04"

ARCH=$(docker run --rm "${BUILD_IMAGE}" uname -m)

echo "=== Stage 1: Build Extension Module on Modern Linux Host (glibc 2.39) ==="
docker run --rm -v "${TMP_DIR}:/work" -w /work "${BUILD_IMAGE}" bash -c '
gcc -shared -fPIC -O2 -o fast_hasher.so fast_hasher.c
gcc -O2 -o loader loader.c -ldl
'
echo "Build complete: fast_hasher.so and test loader compiled."

echo ""
echo "=== Stage 2: Inspect ELF Version Sections via readelf ==="
docker run --rm -v "${TMP_DIR}:/work" -w /work "${BUILD_IMAGE}" bash -c '
echo "Version Needs section (.gnu.version_r):"
readelf -V fast_hasher.so | grep -E "(Version: 1|Name: GLIBC_)" | sed "s/^[ \t]*//"
echo ""
echo "Symbol bindings in .dynsym:"
readelf --dyn-syms --wide fast_hasher.so | grep -E "(compute_hash|strlcpy)" | awk "{print \$4, \$5, \$8}"
'

echo ""
echo "=== Stage 3: Audit Wheel Against manylinux_2_28 Policy ==="
docker run --rm -v "${TMP_DIR}:/work" -w /work "${BUILD_IMAGE}" bash -c "
python3 audit_wheel.py show fast_hasher.so ${ARCH}
echo ''
echo 'Attempting auditwheel repair with target policy manylinux_2_28_${ARCH}...'
python3 audit_wheel.py repair --plat manylinux_2_28_${ARCH} fast_hasher.so dist/ 2>&1 || true
"

echo ""
echo "=== Stage 4: Reproduce Deployment Crash on Older Host (glibc 2.35) ==="
echo "[Execution] Attempting dlopen() of unpatched module on glibc 2.35 container..."
set +e
docker run --rm -v "${TMP_DIR}:/work" -w /work "${DEPLOY_IMAGE}" /work/loader /work/fast_hasher.so 2>&1
LOAD_EXIT=$?
set -e
echo "Exit status: ${LOAD_EXIT}"

echo ""
echo "=== Stage 5: Remediate GLIBC Leak via Portable Source Fallback ==="
echo "[Build] Recompiling fast_hasher.so with portable fallback (-DPORTABLE=1)..."
docker run --rm -v "${TMP_DIR}:/work" -w /work "${BUILD_IMAGE}" bash -c '
gcc -shared -fPIC -O2 -DPORTABLE=1 -o fast_hasher_fixed.so fast_hasher.c
'
echo "Version Needs section (.gnu.version_r) for fixed module:"
docker run --rm -v "${TMP_DIR}:/work" -w /work "${BUILD_IMAGE}" bash -c '
readelf -V fast_hasher_fixed.so | grep -E "(Version: 1|Name: GLIBC_)" | sed "s/^[ \t]*//"
'

echo ""
echo "=== Stage 6: Verify Auditwheel Repair and Deployment Execution ==="
docker run --rm -v "${TMP_DIR}:/work" -w /work "${BUILD_IMAGE}" bash -c "
python3 audit_wheel.py repair --plat manylinux_2_28_${ARCH} fast_hasher_fixed.so dist/
"
echo "[Execution] Loading repaired module on deployment target (glibc 2.35)..."
docker run --rm -v "${TMP_DIR}:/work" -w /work "${DEPLOY_IMAGE}" /work/loader /work/fast_hasher_fixed.so
