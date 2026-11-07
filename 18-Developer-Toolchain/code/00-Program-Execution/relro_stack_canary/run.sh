#!/usr/bin/env bash
# requires: linux
# Sections 1 to 4 run on any Linux architecture (recorded for linux-aarch64 and linux-amd64).
# Section 5 needs an AArch64 host: the harness has no architecture token, so other
# architectures print a skip line there instead.
set -euo pipefail

CC="${CC:-gcc}"

# Build shared library
"${CC}" -fPIC -shared libdummy.c -o libdummy.so

# 1. Stack protector binaries
"${CC}" -O0 -Wno-stringop-overflow -fstack-protector-strong main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -o main_canary
"${CC}" -O0 -Wno-stringop-overflow -fno-stack-protector main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -o main_nocanary

# 2. RELRO binaries
"${CC}" -O0 -Wno-stringop-overflow -Wl,-z,relro,-z,now main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -o main_full
"${CC}" -O0 -Wno-stringop-overflow -Wl,-z,relro,-z,lazy main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -o main_partial
"${CC}" -O0 -Wno-stringop-overflow -Wl,-z,norelro,-z,lazy main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -o main_norelro

# 3. Executable stack binary
"${CC}" -O0 -Wno-stringop-overflow -z execstack main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -o main_execstack

# 4. Branch protection (AArch64 only): objects with and without it, and linked executables
if [ "$(uname -m)" = aarch64 ]; then
    "${CC}" -c -O0 -Wno-stringop-overflow -mbranch-protection=none main_test.c -o main_nobti.o
    "${CC}" -c -O0 -Wno-stringop-overflow -mbranch-protection=standard main_test.c -o main_bti.o
    "${CC}" -O0 -Wno-stringop-overflow -mbranch-protection=standard main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -o main_bti_plain
    "${CC}" -O0 -Wno-stringop-overflow -mbranch-protection=standard main_test.c -L. -ldummy -Wl,-rpath,'$ORIGIN' -Wl,-z,force-bti -o main_bti_forced
fi

echo "=== 1. Stack Canary Verification ==="
echo -n "-fstack-protector-strong, safe input: "; ./main_canary canary-safe
echo -n "-fstack-protector-strong, 64 bytes into 16: "; ./main_canary canary-smash
echo -n "-fno-stack-protector, safe input: "; ./main_nocanary canary-safe
echo -n "-fno-stack-protector, 64 bytes into 16: "; ./main_nocanary canary-smash

echo "=== 2. PT_GNU_STACK Segment Flags ==="
echo -n "Default stack: "
readelf -l main_full | grep GNU_STACK -A 1 | tail -n 1 | awk '{print $3}'
echo -n "-z execstack: "
readelf -l main_execstack | grep GNU_STACK -A 1 | tail -n 1 | awk '{print $3}'
echo -n "Default stack, run code on it: "; ./main_full stack-exec
echo -n "-z execstack, run code on it: "; ./main_execstack stack-exec

echo "=== 3. PT_GNU_RELRO Segment and Section Layout ==="
echo -n "Full RELRO: "
readelf -l main_full | grep -q GNU_RELRO && echo -n "PT_GNU_RELRO present, " || echo -n "no RELRO, "
readelf -d main_full | grep -q BIND_NOW && echo "BIND_NOW present" || echo "lazy binding"
readelf -S main_full | grep -E '\.got' | awk '{print $2, $3}'

echo -n "Partial RELRO: "
readelf -l main_partial | grep -q GNU_RELRO && echo -n "PT_GNU_RELRO present, " || echo -n "no RELRO, "
readelf -d main_partial | grep -q BIND_NOW && echo "BIND_NOW present" || echo "lazy binding"
readelf -S main_partial | grep -E '\.got' | awk '{print $2, $3}'

echo -n "No RELRO: "
readelf -l main_norelro | grep -q GNU_RELRO && echo -n "PT_GNU_RELRO present, " || echo -n "no RELRO, "
readelf -d main_norelro | grep -q BIND_NOW && echo "BIND_NOW present" || echo "lazy binding"
readelf -S main_norelro | grep -E '\.got' | awk '{print $2, $3}'

echo "=== 4. Dynamic GOT Overwrite Protection ==="
echo "Full RELRO (target_func):"
./main_full relro-func
echo "Partial RELRO (target_func):"
./main_partial relro-func
echo "Partial RELRO (target_data_ptr):"
./main_partial relro-data
echo "No RELRO (target_func):"
./main_norelro relro-func

echo "=== 5. Branch Protection (BTI and PAC) Properties ==="
if [ "$(uname -m)" != aarch64 ]; then
    echo "skipped: -mbranch-protection needs an AArch64 host"
    exit 0
fi
props() { readelf -n "$1" | grep -o "AArch64 feature: .*" || echo "no AArch64 feature note"; }
echo -n "Object, -mbranch-protection=none: "; props main_nobti.o
echo -n "Object, -mbranch-protection=standard: "; props main_bti.o
echo -n "Prologue instruction: "
objdump -d -j .text main_bti.o | grep "paciasp" | head -n 1 | awk '{print $2, $3}'
echo -n "Epilogue instruction: "
objdump -d -j .text main_bti.o | grep "autiasp" | head -n 1 | awk '{print $2, $3}'
echo -n "paciasp count, none: "; objdump -d -j .text main_nobti.o | grep -c "paciasp" || true
echo -n "Executable, standard: "; props main_bti_plain
echo -n "Executable, standard plus -z force-bti: "; props main_bti_forced
