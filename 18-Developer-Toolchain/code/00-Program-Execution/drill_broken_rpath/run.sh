#!/usr/bin/env bash
# requires: linux
set -euo pipefail

# The interposition demo is recorded with GCC: it keeps a default-visibility call inside a -fPIC DSO
# as a PLT call. Another compiler may inline compute_hash and hide the bug, so CC is not overridable.
CC=gcc

# host_app exits 0 for the expected hash, 3 for the interposed value, 4 for anything else.
# launch <label> <success|mismatch> runs it and aborts the lab if the outcome is not the one the stage teaches.
launch() {
    echo "[Execution] Launching host_app with $1..."
    local rc=0
    ./host_app || rc=$?
    case "$2:$rc" in
        success:0|mismatch:3) ;;
        *) echo "lab check failed: expected $2 from host_app, exit status was $rc" >&2; exit 1 ;;
    esac
}

# host_trace <LD_DEBUG categories> runs host_app under the loader trace; its exit status is checked by launch.
host_trace() { { LD_DEBUG="$1" ./host_app 2>&1 || true; }; }

# exports <elf>: defined compute_hash and internal_calc_checksum entries of .dynsym as "type bind visibility name".
# readelf --wide keeps full names; a readelf failure aborts the lab through pipefail.
exports() {
    readelf --dyn-syms --wide "$1" \
        | awk '$7 != "UND" && $8 ~ /^(compute_hash|internal_calc_checksum)(@|$)/ {print $4, $5, $6, $8}' \
        | sort
}

# show_exports <elf>: the entries above, then a definite verdict on compute_hash.
# The public entry point must always stay exported, otherwise the fix broke the library instead of hiding the helper.
show_exports() {
    local syms
    syms=$(exports "$1")
    printf '%s\n' "$syms"
    case "$syms" in *internal_calc_checksum*) ;; *) echo "lab check failed: internal_calc_checksum not exported" >&2; exit 1 ;; esac
    case "$syms" in *compute_hash*) echo "compute_hash: exported" ;; *) echo "compute_hash: not exported" ;; esac
}

# host_bindings: how many compute_hash references the loader bound to host_app.
host_bindings() {
    host_trace bindings | awk '/binding file .* to \.\/host_app/ && /compute_hash/ {n++} END {print n + 0}'
}

# Clean previous build artifacts
rm -rf plugins host_app

mkdir -p plugins/lib

echo "=== Stage 1: Reproduce Transitive DT_RUNPATH Lookup Failure ==="
# Build sub-dependency without embedded rpath
"$CC" -shared -fPIC -o plugins/lib/libinternal_math.so internal_math.c

# Build plugin depending on libinternal_math, but without rpath
"$CC" -shared -fPIC -o plugins/libplugin.so plugin.c -Lplugins/lib -linternal_math

# Build host executable with RUNPATH to plugins and plugins/lib
# -Wl,-rpath-link satisfies the static linker without baking plugins/lib into libplugin.so
"$CC" -rdynamic -o host_app host_app.c -Lplugins -lplugin \
    -Wl,-rpath-link,plugins/lib \
    -Wl,-rpath,'$ORIGIN/plugins:$ORIGIN/plugins/lib'

echo "[Execution] Launching host_app with unpatched plugin..."
set +e
./host_app 2>&1
EXIT_CODE=$?
set -e
echo "Exit status: $EXIT_CODE"

echo ""
echo "=== Stage 2: Inspect Dynamic Tags (DT_RPATH vs DT_RUNPATH) ==="
echo "host_app dynamic tags:"
readelf -d host_app | grep -E '(RPATH|RUNPATH|NEEDED)' | sed -e 's/^[ \t]*//'
echo "plugins/libplugin.so dynamic tags:"
readelf -d plugins/libplugin.so | grep -E '(RPATH|RUNPATH|NEEDED)' | sed -e 's/^[ \t]*//'

echo ""
echo "=== Stage 3: Trace Dynamic Linker Search Paths (LD_DEBUG=libs) ==="
echo "Loader search trace for libinternal_math.so:"
set +e
LD_DEBUG=libs ./host_app 2>&1 | grep "find library=libinternal_math.so" -A 4 | sed -e 's/^[ \t]*[0-9]*:[ \t]*//' -e 's/\[[0-9]*\]//g'
set -e

echo ""
echo "=== Stage 4: Fix 1 - Embed \$ORIGIN/lib RUNPATH in libplugin.so ==="
echo "[Build] Relinking libplugin.so with -Wl,-rpath,'\$ORIGIN/lib'..."
"$CC" -shared -fPIC -o plugins/libplugin.so plugin.c -Lplugins/lib -linternal_math -Wl,-rpath,'$ORIGIN/lib'

echo "plugins/libplugin.so updated dynamic tags:"
readelf -d plugins/libplugin.so | grep -E '(RPATH|RUNPATH|NEEDED)' | sed -e 's/^[ \t]*//'

launch "fixed plugin RUNPATH" mismatch

echo ""
echo "=== Stage 5: Diagnose Symbol Preemption (LD_DEBUG=bindings) ==="
echo "Symbol binding trace:"
# The trace names the referencing object by absolute path; keep its basename so the harness's /TMP mask does not erase it.
host_trace bindings | grep "binding.*compute_hash" \
    | sed -e 's/^[ \t]*[0-9]*:[ \t]*//' -e 's/\[[0-9]*\]//g' -e 's#binding file [^ ]*/#binding file #' -e 's/  */ /g'
echo "compute_hash bindings resolved to host_app: $(host_bindings)"

echo "Symbol visibility in host_app dynamic symbol table (.dynsym):"
exports host_app

echo "Symbol visibility in libinternal_math.so dynamic symbol table (.dynsym):"
exports plugins/lib/libinternal_math.so

echo ""
echo "=== Stage 6: Fix 2 - Restructure Symbol Visibility and Local Binding ==="
echo "Applying Fix 2A: Compile libinternal_math with -fvisibility=hidden"
"$CC" -shared -fPIC -fvisibility=hidden -DEXPORT_API -o plugins/lib/libinternal_math.so internal_math.c

echo "Dynamic symbols exported by updated libinternal_math.so (.dynsym):"
show_exports plugins/lib/libinternal_math.so
launch "hidden visibility" success
echo "compute_hash bindings resolved to host_app: $(host_bindings)"

echo ""
echo "Applying Fix 2B: Rebuild with Linker Script (--version-script)"
# Recompile internal_math without hidden flag to demonstrate version script decoupling
"$CC" -shared -fPIC -Wl,--version-script=version_script.map -o plugins/lib/libinternal_math.so internal_math.c
echo "Dynamic symbols exported with version script (.dynsym):"
show_exports plugins/lib/libinternal_math.so
launch "version script DSO" success
echo "compute_hash bindings resolved to host_app: $(host_bindings)"

echo ""
echo "Applying Fix 2C: Rebuild with Direct Binding (-Wl,-Bsymbolic-functions)"
"$CC" -shared -fPIC -Wl,-Bsymbolic-functions -o plugins/lib/libinternal_math.so internal_math.c
echo "Dynamic symbols exported with -Bsymbolic-functions (.dynsym):"
show_exports plugins/lib/libinternal_math.so
launch "-Bsymbolic-functions DSO" success
echo "compute_hash bindings resolved to host_app: $(host_bindings)"
