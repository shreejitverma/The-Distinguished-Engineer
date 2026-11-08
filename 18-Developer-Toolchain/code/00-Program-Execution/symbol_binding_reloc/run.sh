#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

CC="${CC:-gcc}"
PYTHON="${PYTHON:-python3}"
ARCH="$(uname -m)"

# symtab_row FILE TABLE SYMBOL: the readelf row for SYMBOL in the named symbol table (.symtab or .dynsym) only.
# `readelf -s` prints both tables, so grepping its whole output would mix them.
symtab_row() {
    readelf -sW "$1" | awk -v tab="'$2'" -v sym="$3" \
        'index($0, "Symbol table " tab) == 1 { on = 1; next } /^Symbol table/ { on = 0 } on && $8 == sym { print; exit }'
}

# section_of FILE NDX: the name of the section header at index NDX.
section_of() {
    readelf -SW "$1" | sed -n "s/^ *\[ *$2\] \([^ ]*\) .*/\1/p"
}

# --- Step 1: Archive Member Extraction Order ---
echo "=== 1. Archive Member Extraction Order ==="

"${CC}" -c "${SCRIPT_DIR}/archive_main.c" -o "${BUILD_DIR}/main.o"
"${CC}" -c "${SCRIPT_DIR}/archive_alpha.c" -o "${BUILD_DIR}/alpha.o"
"${CC}" -c "${SCRIPT_DIR}/archive_beta.c" -o "${BUILD_DIR}/beta.o"

ar cr "${BUILD_DIR}/libalpha.a" "${BUILD_DIR}/alpha.o"
ar cr "${BUILD_DIR}/libbeta.a" "${BUILD_DIR}/beta.o"

echo "Linking: main.o libbeta.a libalpha.a"
if "${CC}" "${BUILD_DIR}/main.o" -L"${BUILD_DIR}" -lbeta -lalpha -o "${BUILD_DIR}/app1" 2>"${BUILD_DIR}/err1.log"; then
    echo "Result: UNEXPECTED SUCCESS"
else
    if grep -q "undefined reference to.*beta" "${BUILD_DIR}/err1.log"; then
        echo "Result: FAIL (undefined reference to 'beta')"
    else
        echo "Result: FAIL (linker error)"
    fi
fi

echo "Linking: main.o libalpha.a libbeta.a"
"${CC}" "${BUILD_DIR}/main.o" -L"${BUILD_DIR}" -lalpha -lbeta -o "${BUILD_DIR}/app2"
set +e
"${BUILD_DIR}/app2"
RET2=$?
set -e
echo "Result: SUCCESS (exit code ${RET2})"

echo "Linking: main.o --start-group libbeta.a libalpha.a --end-group"
"${CC}" "${BUILD_DIR}/main.o" -Wl,--start-group -L"${BUILD_DIR}" -lbeta -lalpha -Wl,--end-group -o "${BUILD_DIR}/app3"
set +e
"${BUILD_DIR}/app3"
RET3=$?
set -e
echo "Result: SUCCESS (exit code ${RET3})"

# --- Step 2: Symbol Binding and Precedence Rules ---
echo ""
echo "=== 2. Symbol Binding and Precedence Rules ==="

"${CC}" -c "${SCRIPT_DIR}/binding_weak.c" -o "${BUILD_DIR}/binding_weak.o"
"${CC}" -c "${SCRIPT_DIR}/binding_strong.c" -o "${BUILD_DIR}/binding_strong.o"
"${CC}" -c "${SCRIPT_DIR}/binding_strong2.c" -o "${BUILD_DIR}/binding_strong2.o"

echo "Weak symbol overridden by strong symbol:"
"${CC}" "${BUILD_DIR}/binding_strong.o" "${BUILD_DIR}/binding_weak.o" -o "${BUILD_DIR}/app_binding"
set +e
"${BUILD_DIR}/app_binding"
RET_BIND=$?
set -e
echo "Result: SUCCESS (priority = ${RET_BIND})"

echo "Two conflicting strong symbols:"
if "${CC}" "${BUILD_DIR}/binding_strong.o" "${BUILD_DIR}/binding_strong2.o" -o "${BUILD_DIR}/app_dup" 2>"${BUILD_DIR}/err_dup.log"; then
    echo "Result: UNEXPECTED SUCCESS"
else
    if grep -q "multiple definition of.*priority" "${BUILD_DIR}/err_dup.log"; then
        echo "Result: FAIL (multiple definition of 'priority')"
    else
        echo "Result: FAIL (multiple definition)"
    fi
fi

echo "Common symbols with -fcommon:"
"${CC}" -fcommon -c "${SCRIPT_DIR}/common_a.c" -o "${BUILD_DIR}/common_a.o"
"${CC}" -fcommon -c "${SCRIPT_DIR}/common_b.c" -o "${BUILD_DIR}/common_b.o"
"${CC}" "${BUILD_DIR}/common_a.o" "${BUILD_DIR}/common_b.o" -o "${BUILD_DIR}/app_comm"
COMM_ROW="$(symtab_row "${BUILD_DIR}/app_comm" .symtab shared_comm)"
COMM_SZ="$(echo "${COMM_ROW}" | awk '{print $3}')"
COMM_SEC="$(section_of "${BUILD_DIR}/app_comm" "$(echo "${COMM_ROW}" | awk '{print $7}')")"
echo "Result: SUCCESS (merged common symbol size = ${COMM_SZ} bytes in ${COMM_SEC})"

echo "Common symbols with -fno-common:"
"${CC}" -fno-common -c "${SCRIPT_DIR}/common_a.c" -o "${BUILD_DIR}/common_a_nocomm.o"
"${CC}" -fno-common -c "${SCRIPT_DIR}/common_b.c" -o "${BUILD_DIR}/common_b_nocomm.o"
if "${CC}" "${BUILD_DIR}/common_a_nocomm.o" "${BUILD_DIR}/common_b_nocomm.o" -o "${BUILD_DIR}/app_nocomm" 2>"${BUILD_DIR}/err_nocomm.log"; then
    echo "Result: UNEXPECTED SUCCESS"
else
    if grep -q "multiple definition of.*shared_comm" "${BUILD_DIR}/err_nocomm.log"; then
        echo "Result: FAIL (multiple definition of 'shared_comm')"
    else
        echo "Result: FAIL (multiple definition)"
    fi
fi

# --- Step 3: Symbol Visibility and Symbol Tables ---
echo ""
echo "=== 3. Symbol Visibility and Symbol Tables ==="

"${CC}" -fPIC -c "${SCRIPT_DIR}/vis.c" -o "${BUILD_DIR}/vis.o"
"${CC}" -shared "${BUILD_DIR}/vis.o" -o "${BUILD_DIR}/libvis.so"

echo "Relocatable object .symtab:"
for sym in sym_default sym_hidden sym_protected; do
    ROW="$(symtab_row "${BUILD_DIR}/vis.o" .symtab "${sym}")"
    BIND="$(echo "${ROW}" | awk '{print $5}')"
    VIS="$(echo "${ROW}" | awk '{print $6}')"
    echo "${sym}: bind=${BIND}, vis=${VIS}"
done

echo "Shared library .symtab:"
for sym in sym_default sym_hidden sym_protected; do
    ROW="$(symtab_row "${BUILD_DIR}/libvis.so" .symtab "${sym}")"
    BIND="$(echo "${ROW}" | awk '{print $5}')"
    VIS="$(echo "${ROW}" | awk '{print $6}')"
    echo "${sym}: bind=${BIND}, vis=${VIS}"
done

echo "Shared library .dynsym (exported for dynamic linking):"
for sym in sym_default sym_hidden sym_protected; do
    ENTRY="$(symtab_row "${BUILD_DIR}/libvis.so" .dynsym "${sym}")"
    if [ -n "${ENTRY}" ]; then
        BIND=$(echo "${ENTRY}" | awk '{print $5}')
        VIS=$(echo "${ENTRY}" | awk '{print $6}')
        echo "${sym}: bind=${BIND}, vis=${VIS}"
    else
        echo "${sym}: ABSENT"
    fi
done

# --- Step 4: Static Relocation Calculations ---
echo ""
echo "=== 4. Static Relocation Calculations ==="

if [ "${ARCH}" = "aarch64" ] || [ "${ARCH}" = "arm64" ]; then
    "${CC}" -c "${SCRIPT_DIR}/reloc_aarch64.s" -o "${BUILD_DIR}/reloc_asm.o"
else
    "${CC}" -c "${SCRIPT_DIR}/reloc_x86_64.s" -o "${BUILD_DIR}/reloc_asm.o"
fi

"${CC}" -fno-pic -fno-pie -c "${SCRIPT_DIR}/reloc_target.c" -o "${BUILD_DIR}/reloc_target.o"
"${CC}" -no-pie "${BUILD_DIR}/reloc_asm.o" "${BUILD_DIR}/reloc_target.o" -o "${BUILD_DIR}/app_reloc"

"${PYTHON}" "${SCRIPT_DIR}/verify_relocs.py" "${BUILD_DIR}/app_reloc" "${BUILD_DIR}/reloc_asm.o" "${BUILD_DIR}/reloc_target.o"
