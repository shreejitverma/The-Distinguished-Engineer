#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT
cd "${BUILD_DIR}"

CC="${CC:-gcc}"
FAILED=0

# check NAME CMD...: print "ok   NAME" when CMD succeeds, else "FAIL NAME"; any FAIL makes the lab exit 1.
check() {
    local name="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo "ok   ${name}"
    else
        echo "FAIL ${name}"
        FAILED=1
    fi
}

# Disassembly of one function, one instruction per line, addresses and alignment nops dropped.
disasm() {
    objdump -d --no-show-raw-insn "$1" \
        | awk -v fn="<$2>:" '$2 == fn { p = 1; next } /^$/ { p = 0 } p' \
        | sed -E 's/^[[:space:]]*[0-9a-f]+:[[:space:]]*//; s/[[:space:]]+$//; s/\t/ /g' \
        | grep -v '^nop$'
}

elf_type() { readelf -h "$1" | sed -nE 's/^[[:space:]]*Type:[[:space:]]+//p'; }
interp_count() { readelf -lW "$1" | grep -c 'INTERP' || true; }

"${CC}" -O2 -static-pie -o static-pie "${SCRIPT_DIR}/resolve.c" 2> static-pie.link
"${CC}" -O2 -static -no-pie -o static-nopie "${SCRIPT_DIR}/resolve.c" 2> /dev/null
"${CC}" -O2 -fPIE -pie -o dynamic-pie "${SCRIPT_DIR}/resolve.c"
LOADER="$(readelf -lW dynamic-pie | sed -nE 's/.*Requesting program interpreter: (.*)\]$/\1/p')"

echo "== Dynamic loader knobs"
tunable() { "${LOADER}" --list-tunables | grep -o 'glibc.rtld.optional_static_tls: 0x[0-9a-f]*'; }
default_tls="$(tunable)"
echo "default: ${default_tls}"
check "optional_static_tls default" test "${default_tls}" = "glibc.rtld.optional_static_tls: 0x200"
override_tls="$(GLIBC_TUNABLES=glibc.rtld.optional_static_tls=2048 tunable)"
echo "GLIBC_TUNABLES=glibc.rtld.optional_static_tls=2048: ${override_tls}"
check "optional_static_tls override" test "${override_tls}" = "glibc.rtld.optional_static_tls: 0x800"
LD_DEBUG=help ./dynamic-pie > ld_debug_help.txt 2>&1 || true
check "LD_DEBUG help lists statistics" grep -qE '^ +statistics ' ld_debug_help.txt
check "LD_DEBUG help lists no timing option" bash -c '! grep -qE "^ +timing " ld_debug_help.txt'
LD_DEBUG=timing ./dynamic-pie 2> ld_debug_timing.txt > /dev/null || true
echo "LD_DEBUG=timing: $(grep -m1 'timing' ld_debug_timing.txt)"
check "LD_DEBUG=timing is rejected" grep -q "debug option \`timing' unknown" ld_debug_timing.txt
LD_DEBUG=statistics ./dynamic-pie 2> ld_debug_stats.txt > /dev/null
echo "LD_DEBUG=statistics labels:"
sed -nE 's/^[[:space:]]*[0-9]+:[[:space:]]+([a-z][a-z ]*[a-z]):.*/\1/p' ld_debug_stats.txt | sort -u
check "LD_DEBUG=statistics prints relocation counts" grep -q 'number of relocations:' ld_debug_stats.txt
echo "LD_DEBUG=statistics startup-time lines: $(grep -c 'startup time' ld_debug_stats.txt || true)"

echo "== ADR-001: static PIE"
for bin in static-pie static-nopie dynamic-pie; do
    echo "${bin}: ELF type $(elf_type "${bin}"); PT_INTERP headers $(interp_count "${bin}")"
done
check "static-pie ELF type is DYN (PIE)" test "$(elf_type static-pie)" = "DYN (Position-Independent Executable file)"
check "static non-PIE ELF type is EXEC" test "$(elf_type static-nopie)" = "EXEC (Executable file)"
check "dynamic PIE ELF type is DYN (PIE)" test "$(elf_type dynamic-pie)" = "DYN (Position-Independent Executable file)"
check "static-pie has no PT_INTERP" test "$(interp_count static-pie)" = 0
check "static-pie keeps PT_DYNAMIC" bash -c 'readelf -lW static-pie | grep -q " DYNAMIC "'
check "dynamic PIE has one PT_INTERP" test "$(interp_count dynamic-pie)" = 1
callers="$(objdump -d --no-show-raw-insn static-pie \
    | awk '/^[0-9a-f]+ <.*>:$/ { fn = $2 } /bl[[:space:]].*<_dl_relocate_static_pie>/ { print fn }' \
    | tr -d '<>:' | sort -u | paste -sd, -)"
echo "caller of _dl_relocate_static_pie: ${callers}"
check "_dl_relocate_static_pie is called from __libc_start_main" test "${callers}" = "__libc_start_main_impl"
check "_start itself does not call _dl_relocate_static_pie" bash -c '! objdump -d --no-show-raw-insn static-pie | awk "/<_start>:\$/{p=1;next} /^\$/{p=0} p" | grep -q _dl_relocate_static_pie'
check "static-pie has relative relocations" bash -c 'readelf -rW static-pie | grep -q "R_AARCH64_RELATIVE"'
echo "-- ldd"
ldd_out="$(ldd ./static-pie 2>&1)" && ldd_rc=0 || ldd_rc=$?
echo "ldd static-pie: $(echo "${ldd_out}" | sed -E 's/^[[:space:]]+//'); exit ${ldd_rc}"
check "ldd static-pie exit status" test "${ldd_rc}" = 0
check "ldd static-pie says statically linked" test "$(echo "${ldd_out}" | sed -E 's/^[[:space:]]+//')" = "statically linked"
ldd_out="$(ldd ./static-nopie 2>&1)" && ldd_rc=0 || ldd_rc=$?
echo "ldd static non-PIE: $(echo "${ldd_out}" | sed -E 's/^[[:space:]]+//'); exit ${ldd_rc}"
check "ldd static non-PIE exit status" test "${ldd_rc}" = 1
check "ldd dynamic PIE lists libc.so.6" bash -c 'ldd ./dynamic-pie | grep -q "libc.so.6 => "'
echo "-- NSS: the files and dns services are built into libc since glibc 2.34"
echo "hosts line: $(grep -E '^hosts:' /etc/nsswitch.conf | tr -s ' ')"
echo "link warning: $(grep -o "Using 'getaddrinfo' in statically linked applications.*" static-pie.link)"
check "static getaddrinfo link warning" grep -q "Using 'getaddrinfo' in statically linked applications" static-pie.link
static_run="$(./static-pie)"
echo "static-pie run: ${static_run}"
check "static-pie resolves localhost" grep -q 'getaddrinfo: resolved;' <<< "${static_run}"
check "static-pie maps no libnss object" grep -q 'libnss objects mapped: 0;' <<< "${static_run}"
dynamic_run="$(./dynamic-pie)"
echo "dynamic run: ${dynamic_run}"
check "dynamic PIE maps no libnss object" grep -q 'libnss objects mapped: 0;' <<< "${dynamic_run}"

echo "== ADR-002: leaf frame pointers"
for flags in "-fomit-frame-pointer" "-fno-omit-frame-pointer" "-fno-omit-frame-pointer -mno-omit-leaf-frame-pointer"; do
    echo "-- leaf, ${flags}"
    # shellcheck disable=SC2086
    "${CC}" -O2 -fno-stack-protector ${flags} -c "${SCRIPT_DIR}/leaf.c" -o leaf.o
    disasm leaf.o leaf | tee leaf.s
    case "${flags}" in
        *-mno-omit-leaf-frame-pointer) want=1 ;;
        *) want=0 ;;
    esac
    check "leaf frame with ${flags}" test "$(grep -c '^stp x29, x30' leaf.s || true)" = "${want}"
done
# frame_setup OBJ FN: the instructions that build the AArch64 frame record (save x29 and x30, point x29 at it).
frame_setup() { disasm "$1" "$2" | grep -E '^stp x29, x30|^(mov|add) x29, sp' || true; }
for spec in "leaf_stack:-fno-omit-frame-pointer:0" \
    "leaf_stack:-fno-omit-frame-pointer -mno-omit-leaf-frame-pointer:2" \
    "nonleaf:-fno-omit-frame-pointer:2"; do
    fn="${spec%%:*}"
    rest="${spec#*:}"
    flags="${rest%:*}"
    want="${rest##*:}"
    echo "-- ${fn}, ${flags} (frame setup instructions)"
    # shellcheck disable=SC2086
    "${CC}" -O2 -fno-stack-protector ${flags} -c "${SCRIPT_DIR}/leaf.c" -o leaf.o
    frame_setup leaf.o "${fn}" | tee frame.s
    check "${fn} frame setup with ${flags}" test "$(grep -c . frame.s || true)" = "${want}"
done

exit "${FAILED}"
