#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
cd "${WORK_DIR}"

CC="${CC:-gcc}"

# 1. Compile base library and interposer
"$CC" -shared -fPIC -o libmath.so "${SCRIPT_DIR}/libmath.c"
"$CC" -shared -fPIC -o libinterpose.so "${SCRIPT_DIR}/interposer.c" -ldl

# 2. Compile test executable with lazy binding
"$CC" -fPIE -pie -Wl,-z,lazy -o test_got "${SCRIPT_DIR}/test_got.c" -L. -lmath -ldl -Wl,-rpath,'$ORIGIN'

echo "=== 1. Lazy Binding Mechanics ==="
./test_got

echo ""
echo "=== 2. Immediate Binding (LD_BIND_NOW=1) ==="
LD_BIND_NOW=1 ./test_got

echo ""
echo "=== 3. Dynamic Library Interposition (LD_PRELOAD) ==="
LD_PRELOAD=./libinterpose.so ./test_got

# 4. Search Path Order: DT_RPATH vs DT_RUNPATH vs LD_LIBRARY_PATH
mkdir -p lib_rpath lib_env
"$CC" -shared -fPIC -o lib_rpath/libgreeting.so "${SCRIPT_DIR}/greeting_rpath.c"
"$CC" -shared -fPIC -o lib_env/libgreeting.so "${SCRIPT_DIR}/greeting_env.c"

# app_rpath uses DT_RPATH (--disable-new-dtags)
"$CC" -fPIE -pie -Wl,--disable-new-dtags -Wl,-rpath,'$ORIGIN/lib_rpath' \
    -o app_rpath "${SCRIPT_DIR}/app_greeting.c" -Llib_rpath -lgreeting

# app_runpath uses DT_RUNPATH (--enable-new-dtags)
"$CC" -fPIE -pie -Wl,--enable-new-dtags -Wl,-rpath,'$ORIGIN/lib_rpath' \
    -o app_runpath "${SCRIPT_DIR}/app_greeting.c" -Llib_rpath -lgreeting

echo ""
echo "=== 4. Search Path Precedence (RPATH vs RUNPATH) ==="
echo "app_rpath without LD_LIBRARY_PATH:"
./app_rpath
echo "app_rpath with LD_LIBRARY_PATH=lib_env (RPATH overrides LD_LIBRARY_PATH):"
LD_LIBRARY_PATH=lib_env ./app_rpath

echo "app_runpath without LD_LIBRARY_PATH:"
./app_runpath
echo "app_runpath with LD_LIBRARY_PATH=lib_env (LD_LIBRARY_PATH overrides RUNPATH):"
LD_LIBRARY_PATH=lib_env ./app_runpath

echo ""
echo "=== 5. Dynamic Section Tags (readelf -d) ==="
echo "app_rpath dynamic tags:"
readelf -d app_rpath | grep -E '(RPATH|RUNPATH|NEEDED)' | sed -e 's/^[ \t]*//'
echo "app_runpath dynamic tags:"
readelf -d app_runpath | grep -E '(RPATH|RUNPATH|NEEDED)' | sed -e 's/^[ \t]*//'

# 6. Linker defaults on this toolchain: binding mode, RELRO, rpath tag, -Bsymbolic
"$CC" -fPIE -pie -o app_default "${SCRIPT_DIR}/app_greeting.c" -Llib_rpath -lgreeting
"$CC" -fPIE -pie -Wl,-z,lazy -o app_lazy "${SCRIPT_DIR}/app_greeting.c" -Llib_rpath -lgreeting
"$CC" -fPIE -pie -Wl,-rpath,'$ORIGIN/lib_rpath' -o app_defaulttag "${SCRIPT_DIR}/app_greeting.c" -Llib_rpath -lgreeting
"$CC" -shared -fPIC -o libsym_plain.so "${SCRIPT_DIR}/libmath.c"
"$CC" -shared -fPIC -Wl,-Bsymbolic -o libsym_full.so "${SCRIPT_DIR}/libmath.c"
"$CC" -shared -fPIC -Wl,-Bsymbolic-functions -o libsym_funcs.so "${SCRIPT_DIR}/libmath.c"

echo ""
echo "=== 6. Linker Defaults (readelf) ==="
echo "plain gcc -pie link, binding tags:"
readelf -d app_default | grep -E '\((BIND_NOW|FLAGS|FLAGS_1)\)' | sed -e 's/^[ \t]*//' || true
echo "plain gcc -pie link, PT_GNU_RELRO headers: $(readelf -lW app_default | grep -c GNU_RELRO)"
echo "-Wl,-z,lazy link, binding tags:"
readelf -d app_lazy | grep -E '\((BIND_NOW|FLAGS|FLAGS_1)\)' | sed -e 's/^[ \t]*//' || true
echo "-Wl,-rpath with no dtags flag:"
readelf -d app_defaulttag | grep -E '\((RPATH|RUNPATH)\)' | sed -e 's/^[ \t]*//'
echo "-Bsymbolic tags:"
readelf -d libsym_full.so | grep -E '\((SYMBOLIC|FLAGS)\)' | sed -e 's/^[ \t]*//' || true
echo "-Bsymbolic-functions tags:"
readelf -d libsym_funcs.so | grep -E '\((SYMBOLIC|FLAGS)\)' | sed -e 's/^[ \t]*//' || true
echo "no -Bsymbolic tags:"
readelf -d libsym_plain.so | grep -E '\((SYMBOLIC|FLAGS)\)' | sed -e 's/^[ \t]*//' || true
