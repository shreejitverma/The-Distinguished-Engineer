#!/usr/bin/env bash
# requires: linux
set -euo pipefail

# Setup deterministic sandbox
TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT
cd "${TMPDIR}"

CC="${CC:-gcc}"
CXX="${CXX:-g++}"

# Output scrubber for run-to-run invariants
scrub_output() {
    sed -E \
        -e 's/^[[:space:]]*[0-9]+:[[:space:]]*/[PID] /' \
        -e 's|'"${TMPDIR}"'|[DIR]|g' \
        -e 's|`|'"'"'|g'
}

# show CMD...: run CMD, print its merged stdout and stderr scrubbed, then its exit status.
# The status line makes every claim about exit 0 versus exit 127 a checked fact.
show() {
    local out rc
    out="$("$@" 2>&1)" && rc=0 || rc=$?
    printf '%s\n' "${out}" | scrub_output
    echo "exit=${rc}"
}

# ==============================================================================
# Stage 1: Diagnosing Missing DSO and DT_RPATH vs DT_RUNPATH
# ==============================================================================
cat << "EOF" > libcalc.c
int add_numbers(int a, int b) { return a + b; }
EOF

cat << "EOF" > libcalc_override.c
int add_numbers(int a, int b) { return 999; }
EOF

cat << "EOF" > calc_main.c
#include <stdio.h>
extern int add_numbers(int a, int b);
int main(void) {
    printf("Result: %d\n", add_numbers(10, 20));
    return 0;
}
EOF

mkdir -p lib override
$CC -shared -fPIC -o lib/libcalc.so libcalc.c
$CC -shared -fPIC -o override/libcalc.so libcalc_override.c
$CC -o app_norpath calc_main.c -Llib -lcalc
$CC -o app_runpath calc_main.c -Llib -lcalc -Wl,-rpath,'$ORIGIN/lib' -Wl,--enable-new-dtags
$CC -o app_rpath calc_main.c -Llib -lcalc -Wl,-rpath,'$ORIGIN/lib' -Wl,--disable-new-dtags

echo "=== Stage 1: DSO Search Paths and RPATH vs RUNPATH ==="
echo "--- Step 1.1: Missing DSO failure without RPATH/RUNPATH ---"
show ./app_norpath

echo "--- Step 1.2: Diagnostic search trace via LD_DEBUG=libs ---"
(LD_DEBUG=libs ./app_norpath 2>&1 || true) | scrub_output | tr -s '\t ' ' ' | grep -E "(find library=libcalc|search cache|search path|trying file=)" | head -n 7

echo "--- Step 1.3: Successful load via DT_RUNPATH (\$ORIGIN/lib) ---"
show ./app_runpath

echo "--- Step 1.3b: The same trace for app_runpath shows the RUNPATH entry searched first ---"
(LD_DEBUG=libs ./app_runpath 2>&1 || true) | scrub_output | tr -s '\t ' ' ' | grep -E "(find library=libcalc|search path|trying file=)" | head -n 3

echo "--- Step 1.4: LD_LIBRARY_PATH override on DT_RUNPATH ---"
show env LD_LIBRARY_PATH=override ./app_runpath

echo "--- Step 1.5: DT_RPATH precedence over LD_LIBRARY_PATH ---"
show env LD_LIBRARY_PATH=override ./app_rpath

echo "--- Step 1.6: Dynamic tags inspection (readelf -d) ---"
readelf -d app_runpath | grep -E "\((NEEDED|RUNPATH|RPATH)\)" | sed -E "s/0x[0-9a-fA-F]+/0xTAG/g"
readelf -d app_rpath | grep -E "\((NEEDED|RUNPATH|RPATH)\)" | sed -E "s/0x[0-9a-fA-F]+/0xTAG/g"

# ==============================================================================
# Stage 2: Diagnosing Undefined Symbol & Lazy vs Immediate Binding
# ==============================================================================
cat << "EOF" > libcore.c
int core_available(void) { return 42; }
EOF

cat << "EOF" > libengine.c
#include <stdio.h>
extern int core_available(void);
extern int unresolved_kernel(void);

void init_engine(void) {
    fprintf(stderr, "Engine initialized (core=%d)\n", core_available());
}

void compute_task(void) {
    fprintf(stderr, "Task result: %d\n", unresolved_kernel());
}
EOF

cat << "EOF" > engine_main.c
#include <stdio.h>
extern void init_engine(void);
extern void compute_task(void);

int main(void) {
    fprintf(stderr, "Main started\n");
    init_engine();
    fprintf(stderr, "Dispatching task...\n");
    compute_task();
    return 0;
}
EOF

$CC -shared -fPIC -o lib/libcore.so libcore.c
$CC -shared -fPIC -o lib/libengine.so libengine.c -Llib -lcore -Wl,-rpath,'$ORIGIN' -Wl,--enable-new-dtags -Wl,-z,lazy
$CC -o app_lazy engine_main.c -Llib -lengine -Wl,-rpath,'$ORIGIN/lib' -Wl,--enable-new-dtags -Wl,--allow-shlib-undefined -Wl,-z,lazy

echo "=== Stage 2: Undefined Symbols and Immediate Binding ==="
echo "--- Step 2.1: Default lazy binding deferred abort ---"
show ./app_lazy

echo "--- Step 2.2: Immediate binding via LD_BIND_NOW=1 ---"
show env LD_BIND_NOW=1 ./app_lazy

echo "--- Step 2.3: Dynamic symbol table inspection (nm -D) ---"
nm -D lib/libengine.so | grep -E " (U|T) (unresolved_kernel|init_engine)" | awk '{print $(NF-1), $NF}'

# ==============================================================================
# Stage 3: Diagnosing GNU Symbol Version Mismatch
# ==============================================================================
cat << "EOF" > libscore.c
int calculate_score(int x) { return x * 10; }
int calculate_score_v2(int x) { return x * 20; }
EOF

cat << "EOF" > v1.map
VER_1.0 {
    global:
        calculate_score;
    local:
        *;
};
EOF

cat << "EOF" > v2.map
VER_1.0 {
    global:
        calculate_score;
    local:
        *;
};
VER_2.0 {
    global:
        calculate_score_v2;
} VER_1.0;
EOF

cat << "EOF" > score_main.c
#include <stdio.h>
extern int calculate_score_v2(int x);

int main(void) {
    printf("Score: %d\n", calculate_score_v2(5));
    return 0;
}
EOF

mkdir -p lib_v1 lib_v2
$CC -shared -fPIC -o lib_v1/libscore.so libscore.c -Wl,--version-script,v1.map
$CC -shared -fPIC -o lib_v2/libscore.so libscore.c -Wl,--version-script,v2.map
$CC -o app_v2 score_main.c -Llib_v2 -lscore -Wl,-rpath,'$ORIGIN/lib_v2' -Wl,--enable-new-dtags

echo "=== Stage 3: Symbol Versioning and ABI Compatibility ==="
echo "--- Step 3.1: Execution with matching version definition ---"
show ./app_v2

echo "--- Step 3.2: Version mismatch failure against v1 DSO ---"
show env LD_LIBRARY_PATH=lib_v1 ./app_v2

echo "--- Step 3.3: Version requirement trace via LD_DEBUG=versions ---"
(LD_DEBUG=versions LD_LIBRARY_PATH=lib_v1 ./app_v2 2>&1 || true) | scrub_output | grep -E "(checking for version .VER_2.0.|version lookup error)" | head -n 2

echo "--- Step 3.4: Binary version requirements (.gnu.version_r) ---"
readelf -V app_v2 | grep -E '(File:|Name: VER_)' | sed 's/^[[:space:]]*//'

echo "--- Step 3.5: DSO version definitions (.gnu.version_d) ---"
readelf -V lib_v1/libscore.so | grep -E 'Name: VER_' | sed 's/^[[:space:]]*//'

# ==============================================================================
# Stage 4: Diagnosing C++ ABI Mismatch (dual ABI, missing vtable) and Symbol Visibility
# ==============================================================================
cat << "EOF" > vendor.h
#include <string>
int vendor_len(const std::string& s);
EOF

cat << "EOF" > vendor.cpp
#include "vendor.h"
int vendor_len(const std::string& s) { return static_cast<int>(s.size()); }
EOF

cat << "EOF" > abi_main.cpp
#include <cstdio>
#include "vendor.h"
int main() {
    std::printf("len=%d\n", vendor_len(std::string("hello")));
    return 0;
}
EOF

cat << "EOF" > vt_main.cpp
struct MyClass {
    virtual void run();
    virtual ~MyClass() {}
};
int main() {
    MyClass m;
    return 0;
}
EOF

cat << "EOF" > libvis.c
__attribute__((visibility("hidden"))) int hidden_fn(void) { return 1; }
int visible_fn(void) { return hidden_fn() + 1; }
EOF

mkdir -p abi_old abi_new
$CXX -shared -fPIC -D_GLIBCXX_USE_CXX11_ABI=0 -o abi_old/libvendor.so vendor.cpp
$CXX -shared -fPIC -D_GLIBCXX_USE_CXX11_ABI=1 -o abi_new/libvendor.so vendor.cpp
$CXX -o app_abi_ok abi_main.cpp -Labi_new -lvendor -Wl,-rpath,'$ORIGIN/abi_new' -Wl,--enable-new-dtags
$CC -shared -fPIC -o libvis.so libvis.c

echo "=== Stage 4: C++ ABI Mismatch and Symbol Visibility ==="
echo "--- Step 4.1: Exported names differ between the old and new std::string ABI ---"
nm -D --defined-only abi_old/libvendor.so | awk '/vendor_len/ {print "old:", $NF}'
nm -D --defined-only abi_new/libvendor.so | awk '/vendor_len/ {print "new:", $NF}'

echo "--- Step 4.2: Matching ABI runs ---"
show ./app_abi_ok

echo "--- Step 4.3: A new-ABI caller cannot link against the old-ABI library ---"
show $CXX -o app_abi_bad abi_main.cpp -Labi_old -lvendor 2>&1 | grep -E "(undefined reference|^exit=)" | sed -E 's/\(\.text\+0x[0-9a-fA-F]+\)/(.text+0xOFF)/'

echo "--- Step 4.4: Missing key function leaves the vtable undefined ---"
show $CXX -o vt_app vt_main.cpp | grep -E "(undefined reference to .vtable for MyClass.|^exit=)" | sed -E -e 's|^/usr/bin/ld: ||' -e 's/\(\.text[^)]*\)/(.text...)/' | sort -u
$CXX -c -o vt_main.o vt_main.cpp
nm vt_main.o | awk '$1 == "U" && /_ZTV7MyClass/ {print $1, $2}'
echo _ZTV7MyClass | c++filt

echo "--- Step 4.5: Hidden symbols never reach the dynamic symbol table ---"
nm -D --defined-only libvis.so | awk '/(hidden|visible)_fn$/ {print "nm -D:", $2, $3}'
nm libvis.so | awk '/(hidden|visible)_fn$/ {print "nm   :", $2, $3}'
