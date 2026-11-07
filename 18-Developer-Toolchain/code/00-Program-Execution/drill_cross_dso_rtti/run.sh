#!/usr/bin/env bash
# requires: linux
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

LIBCXX="clang++ -stdlib=libc++"
LIBSTDCXX="g++"
FLAGS="-std=c++17 -Wall -Wextra"
HIDDEN="-fvisibility=hidden"
EXPORT='-DERROR_EXPORT=__attribute__((visibility("default")))'
KEYFN="-DUSE_KEY_FUNCTION"

for tool in clang++ g++ readelf; do
    command -v "${tool}" >/dev/null || { echo "missing tool: ${tool}" >&2; exit 2; }
done

# build <out> <toolchain> <flags...> <sources/libs...>: one compile and link, word-split on purpose.
build() {
    local out=$1 tc=$2
    shift 2
    # shellcheck disable=SC2086
    ${tc} ${FLAGS} "$@" -o "${out}"
}

# check <label> <want> <host> <plugin> [host args...]
# Runs the host, prints what it measured, and fails the lab when the outcome is not the expected one.
# want is: caught | fallback | abort
check() {
    local label=$1 want=$2 host=$3 plugin=$4 got status=0 out err
    shift 4
    out=$({ ./"${host}" "./${plugin}" "$@" 2>err.txt; } 2>/dev/null) || status=$?
    err=$(grep -m1 '^libc++abi: terminating' err.txt || true)
    case "${status}:${out}" in
        0:*outcome=caught-PluginError*) got=caught ;;
        0:*outcome=fell-back-to-std-exception*) got=fallback ;;
        134:*) got=abort ;;
        *) got="unexpected(status ${status})" ;;
    esac
    echo "[${label}]"
    echo "${out}" | sed 's/^/  /'
    [ -n "${err}" ] && echo "  stderr: ${err}"
    echo "  exit status ${status}"
    if [ "${got}" != "${want}" ]; then
        echo "LAB FAILURE: ${label}: expected ${want}, got ${got}" >&2
        exit 1
    fi
}

# dynsym_count <so> <pattern>: how many dynamic symbols match.
dynsym_count() { readelf --wide --dyn-syms "$1" | grep -cE "$2" || true; }

echo "=== Drill: Cross-DSO Exception and RTTI Failure ==="

echo "--- 1. Reproduction: real libc++ with -fvisibility=hidden ---"
build libplugin_broken.so "${LIBCXX}" -fPIC -shared ${HIDDEN} plugin.cpp
build host_broken "${LIBCXX}" ${HIDDEN} host.cpp -ldl
build host_broken_global "${LIBCXX}" ${HIDDEN} -DDLMODE=RTLD_GLOBAL host.cpp -ldl
check "libc++ fallback handler present" fallback host_broken libplugin_broken.so
check "libc++ strict, RTLD_LOCAL" abort host_broken libplugin_broken.so --strict
check "libc++ strict, RTLD_GLOBAL" abort host_broken_global libplugin_broken.so --strict

echo "--- 2. Diagnosis: libstdc++ masks the same bug ---"
build libplugin_gnu.so "${LIBSTDCXX}" -fPIC -shared ${HIDDEN} plugin.cpp
build host_gnu "${LIBSTDCXX}" ${HIDDEN} host.cpp -ldl
check "libstdc++ fallback handler present" caught host_gnu libplugin_gnu.so
check "libstdc++ strict" caught host_gnu libplugin_gnu.so --strict

echo "--- 3. Diagnosis: ELF symbol inspection ---"
echo "dynamic RTTI symbols in libplugin_broken.so: $(dynsym_count libplugin_broken.so '_ZT[IS]11PluginError')"
echo "libplugin_broken.so .symtab (bind visibility name):"
readelf --wide --syms libplugin_broken.so | grep -E '_ZT[IS]11PluginError' | awk '{print "  " $5, $6, $8}' | sort
build plugin_hidden.o "${LIBCXX}" -c -fPIC ${HIDDEN} plugin.cpp
build plugin_default.o "${LIBCXX}" -c -fPIC plugin.cpp
echo "plugin.o _ZTI11PluginError, -fvisibility=hidden: $(readelf --wide --syms plugin_hidden.o | awk '$8=="_ZTI11PluginError" {print $5, $6}')"
echo "plugin.o _ZTI11PluginError, default flags:       $(readelf --wide --syms plugin_default.o | awk '$8=="_ZTI11PluginError" {print $5, $6}')"

echo "--- 4. Remediation A: visibility annotation plus --export-dynamic ---"
build libplugin_annotated.so "${LIBCXX}" -fPIC -shared ${HIDDEN} "${EXPORT}" plugin.cpp
build host_annotated_static "${LIBCXX}" ${HIDDEN} "${EXPORT}" host.cpp -ldl
build host_annotated_static_global "${LIBCXX}" ${HIDDEN} "${EXPORT}" -DDLMODE=RTLD_GLOBAL host.cpp -ldl
build host_annotated "${LIBCXX}" ${HIDDEN} "${EXPORT}" -Wl,--export-dynamic host.cpp -ldl
check "A without --export-dynamic, RTLD_LOCAL (control)" abort host_annotated_static libplugin_annotated.so --strict
check "A without --export-dynamic, RTLD_GLOBAL (control)" abort host_annotated_static_global libplugin_annotated.so --strict
check "A with --export-dynamic" caught host_annotated libplugin_annotated.so --strict

echo "--- 5. Remediation B: version script that lists the RTTI symbols ---"
build libplugin_map.so "${LIBCXX}" -fPIC -shared ${HIDDEN} "${EXPORT}" -Wl,--version-script=plugin_export.map plugin.cpp
printf '{ global: trigger_error; get_plugin_typeinfo; local: *; };\n' > plugin_nortti.map
build libplugin_nortti.so "${LIBCXX}" -fPIC -shared ${HIDDEN} "${EXPORT}" -Wl,--version-script=plugin_nortti.map plugin.cpp
build libplugin_maphidden.so "${LIBCXX}" -fPIC -shared ${HIDDEN} -Wl,--version-script=plugin_export.map plugin.cpp
check "B map lists _ZTI/_ZTS" caught host_annotated libplugin_map.so --strict
check "B map omits _ZTI/_ZTS (control)" abort host_annotated libplugin_nortti.so --strict
echo "dynamic RTTI symbols, annotated plugin, map lists them: $(dynsym_count libplugin_map.so '_ZT[IS]11PluginError')"
echo "dynamic RTTI symbols, annotated plugin, map omits them: $(dynsym_count libplugin_nortti.so '_ZT[IS]11PluginError')"
echo "dynamic RTTI symbols, hidden plugin, map lists them:    $(dynsym_count libplugin_maphidden.so '_ZT[IS]11PluginError')"

echo "--- 6. Remediation C: key function in a common shared library ---"
build libcommon.so "${LIBCXX}" -fPIC -shared common.cpp
build libplugin_common.so "${LIBCXX}" -fPIC -shared ${HIDDEN} ${KEYFN} "${EXPORT}" plugin.cpp -L. -lcommon "-Wl,-rpath,\$ORIGIN"
build host_common "${LIBCXX}" ${HIDDEN} ${KEYFN} "${EXPORT}" host.cpp -L. -lcommon -ldl "-Wl,-rpath,\$ORIGIN"
check "C key function in libcommon.so, host not exporting" caught host_common libplugin_common.so --strict
echo "libcommon.so defines _ZTI11PluginError: $(readelf --wide --dyn-syms libcommon.so | awk '$8=="_ZTI11PluginError" && $7!="UND" {n++} END {print n+0}')"
for f in host_common libplugin_common.so; do
    echo "${f} relocations against _ZTI11PluginError: $(readelf --wide --relocs "${f}" | awk '$5=="_ZTI11PluginError" {print $3}' | sort | paste -sd, -)"
    echo "${f} defines _ZTI11PluginError itself: $(readelf --wide --syms "${f}" | awk '$8=="_ZTI11PluginError" && $7!="UND" {n++} END {print n+0}')"
done

echo "=== All drill checks passed ==="
