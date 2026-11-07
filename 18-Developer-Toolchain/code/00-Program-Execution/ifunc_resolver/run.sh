#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CC="${CC:-gcc}"

# Section 6 needs GNU gold, which binutils deprecated in 2.44 and ships only in a separate tarball.
if ! command -v ld.gold >/dev/null 2>&1; then
    echo "ld.gold not found: install binutils-gold, or build gold from the separate binutils-gold tarball" >&2
    exit 1
fi

# 1. GNU Symbol Versioning Execution and Inspection
"${CC}" -shared -fPIC -Wl,--version-script,libcrypto.map libcrypto.c -o libcrypto.so
"${CC}" client_v1.c -L. -lcrypto -Wl,-rpath,. -o client_v1
"${CC}" client_v2.c -L. -lcrypto -Wl,-rpath,. -o client_v2

echo "=== 1. GNU Symbol Versioning Execution ==="
./client_v1
./client_v2

echo "=== 2. Version Definitions in libcrypto.so (.gnu.version_d) ==="
readelf -V libcrypto.so | grep -E "(Index:|Name:|Parent)" | sed "s/^[ \t]*//"
echo "--- exported crypto_hash symbols (@ is non-default, @@ is default) ---"
readelf --dyn-syms -W libcrypto.so | awk '$8 ~ /^crypto_hash@/ {print $8}'

echo "=== 3. Version Requirements (.gnu.version_r) ==="
echo "--- client_v1 ---"
readelf -V client_v1 | grep -A 1 "libcrypto.so" | sed "s/^[ \t]*//"
echo "--- client_v2 ---"
readelf -V client_v2 | grep -A 1 "libcrypto.so" | sed "s/^[ \t]*//"

# 2. STT_GNU_IFUNC Execution and Inspection
"${CC}" -O2 -fPIC -pie ifunc_demo.c -o ifunc_demo

echo "=== 4. STT_GNU_IFUNC Execution and Relocation ==="
./ifunc_demo
readelf -s -W ifunc_demo | grep compute_digest | awk '{print $4, $5, $8}'
readelf -r -W ifunc_demo | grep IRELATIVE | awk '{print $3}'

# 3. Linker Scripts, --gc-sections, and KEEP()
"${CC}" -ffunction-sections -fdata-sections sections_demo.c -o demo_nogc
"${CC}" -ffunction-sections -fdata-sections -Wl,--gc-sections sections_demo.c -o demo_nogc_script
"${CC}" -ffunction-sections -fdata-sections -Wl,-T,augment.ld -Wl,--gc-sections sections_demo.c -o demo_keep

echo "=== 5. Linker Dead Code Elimination and KEEP() ==="
if readelf -s demo_nogc | grep -q dead_code_function; then
    echo "dead_code_function without --gc-sections: retained"
else
    echo "dead_code_function without --gc-sections: discarded"
fi

if readelf -s demo_keep | grep -q dead_code_function; then
    echo "dead_code_function: retained"
else
    echo "dead_code_function: discarded by --gc-sections"
fi

if readelf -S demo_nogc_script | grep -q "\.registry"; then
    echo ".registry without KEEP: retained"
else
    echo ".registry without KEEP: discarded by --gc-sections"
fi

if readelf -S demo_keep | grep -q "\.registry"; then
    echo ".registry with KEEP: preserved in output ELF"
else
    echo ".registry with KEEP: discarded"
fi

# 4. Identical Code Folding (ICF) Pointer Identity
# The --icf=none build is the control: if the compiler had folded the twins, it would show here too.
for mode in none all safe; do
    "${CC}" -O2 -ffunction-sections -fuse-ld=gold -Wl,--icf="${mode}" icf_demo.c -o "icf_${mode}"
done

addr_of() { nm "$1" | awk -v s="$2" '$3 == s {print $1}'; }

echo "=== 6. Identical Code Folding (ICF) Pointer Identity ==="
for mode in none all safe; do
    printf "gold --icf=%s: " "${mode}"
    "./icf_${mode}"
done
for mode in none all safe; do
    if [ "$(addr_of "icf_${mode}" twin_a)" = "$(addr_of "icf_${mode}" twin_b)" ]; then
        echo "gold --icf=${mode} direct-call-only twins: folded to one address"
    else
        echo "gold --icf=${mode} direct-call-only twins: distinct addresses"
    fi
done
