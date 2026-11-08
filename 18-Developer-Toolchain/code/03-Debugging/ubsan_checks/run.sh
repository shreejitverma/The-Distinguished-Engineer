#!/usr/bin/env bash
# UndefinedBehaviorSanitizer check catalog, recovery modes, and trap mode internals.
# requires: macos
# Recorded with Apple clang (the default c++ on macOS); it reads Mach-O with
# nm, otool, objdump, and dwarfdump, so it is gated to macOS hosts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
CXX="${CXX:-c++}"
export LC_ALL=C
unset UBSAN_OPTIONS

# Compile from the lab directory with relative paths so reports name the file only.
cd "${SCRIPT_DIR}"

build() { # build <out> <flags...> -- <sources...>
    local out="$1"; shift
    "${CXX}" -g -Wno-everything "$@" -o "${WORK}/${out}"
}

# Run a binary, then print its reports, summary kinds, stdout, and exit status.
show() { # show <label> <binary> <UBSAN_OPTIONS> <args...>
    local label="$1" bin="$2" opts="$3"; shift 3
    local rc
    # The command substitution keeps bash's own "Abort trap" notice off the terminal.
    rc=$( { UBSAN_OPTIONS="${opts}" "${WORK}/${bin}" "$@" >"${WORK}/out" 2>"${WORK}/err"; echo $?; } 2>/dev/null ) || true
    echo "[${label}]"
    if grep -q 'runtime error:' "${WORK}/err"; then
        grep -E 'runtime error:|note: object is of type' "${WORK}/err" \
            | sed -E 's/0x[0-9a-f]+/0xADDR/g; s/^/  report:  /'
    else
        echo "  report:  (none)"
    fi
    grep -E '^SUMMARY: UndefinedBehaviorSanitizer:' "${WORK}/err" \
        | awk '{print "  summary: " $3}' || true
    sed 's/^/  stdout:  /' "${WORK}/out"
    if [ "${rc}" -gt 128 ]; then
        echo "  exit:    signal SIG$(kill -l $((rc - 128)))"
    else
        echo "  exit:    ${rc}"
    fi
}

reports() { grep -c 'runtime error:' "${WORK}/err" || true; }

echo "== 1. Catalog: -std=c++17 -O0 -fsanitize=undefined, report_error_type=1"
build cat17 -std=c++17 -O0 -fsanitize=undefined ub_catalog.cpp
for c in signed-add int-min-div div-zero shift-exponent shift-negative-base \
         shift-overflow-base array-bounds misaligned-load null-reference \
         null-plus-offset bool-load enum-in-range enum-out-of-range enum-class-unlisted float-cast \
         builtin-ctz nonnull-arg; do
    show "${c}" cat17 report_error_type=1 "${c}"
done

echo
echo "== 2. Not in -fsanitize=undefined: vptr and the integer group"
show "bad-downcast, -fsanitize=undefined" cat17 report_error_type=1 bad-downcast
build catvptr -std=c++17 -O0 -fsanitize=undefined,vptr ub_catalog.cpp
show "bad-downcast, -fsanitize=undefined,vptr" catvptr report_error_type=1 bad-downcast
show "unsigned-wrap, -fsanitize=undefined" cat17 report_error_type=1 unsigned-wrap
show "implicit-truncation, -fsanitize=undefined" cat17 report_error_type=1 implicit-truncation
build catint -std=c++17 -O0 -fsanitize=integer ub_catalog.cpp
show "unsigned-wrap, -fsanitize=integer" catint report_error_type=1 unsigned-wrap
show "implicit-truncation, -fsanitize=integer" catint report_error_type=1 implicit-truncation

echo
echo "== 3. The language mode changes what is UB: shifts under -std=c++20"
build cat20 -std=c++20 -O0 -fsanitize=undefined ub_catalog.cpp
for c in shift-negative-base shift-overflow-base shift-exponent; do
    show "${c}, c++20" cat20 report_error_type=1 "${c}"
done

echo
echo "== 4. One report per source location"
UBSAN_OPTIONS="" "${WORK}/cat17" same-site-loop >"${WORK}/out" 2>"${WORK}/err"
# Three wrapped INT_MIN results sum to 3 * INT_MIN, which shows all three iterations ran.
echo "same-site-loop: $(sed -n 1p "${WORK}/out"); 3 overflows at one location, reports printed: $(reports)"
UBSAN_OPTIONS="" "${WORK}/cat17" signed-add int-min-div shift-exponent >/dev/null 2>"${WORK}/err"
echo "three cases at three locations, reports printed: $(reports)"
for o in -O0 -O2; do
    build "inline${o}" "${o}" -fsanitize=signed-integer-overflow inline_dedup.cpp
    UBSAN_OPTIONS="" "${WORK}/inline${o}" >/dev/null 2>"${WORK}/err"
    echo "always_inline helper overflowing in 3 callers, ${o}: reports printed: $(reports)"
done

echo
echo "== 5. Recovery: what happens after the first report"
show "recover (default build)" cat17 "" signed-add shift-exponent
build catfatal -std=c++17 -O0 -fsanitize=undefined -fno-sanitize-recover=undefined ub_catalog.cpp
show "-fno-sanitize-recover=undefined" catfatal "" signed-add shift-exponent
show "halt_on_error=1" cat17 halt_on_error=1 signed-add shift-exponent
show "halt_on_error=1:abort_on_error=0" cat17 halt_on_error=1:abort_on_error=0 signed-add shift-exponent
show "halt_on_error=1:abort_on_error=0:exitcode=42" cat17 halt_on_error=1:abort_on_error=0:exitcode=42 signed-add
show "missing-return (unrecoverable even in a recover build)" cat17 "" missing-return signed-add

echo
echo "== 6. Runtime suppressions (recoverable checks only)"
printf 'signed-integer-overflow:ub_catalog.cpp\n' >"${WORK}/ubsan.supp"
show "suppress signed-integer-overflow" cat17 "suppressions=${WORK}/ubsan.supp" signed-add shift-exponent

echo
echo "== 7. Trap mode: no runtime, one trap instruction per check kind"
build rt_main -O2 -fsanitize=signed-integer-overflow trap_main.cpp trap_kinds.cpp
build trap_main -O2 -fsanitize=signed-integer-overflow -fsanitize-trap=signed-integer-overflow trap_main.cpp trap_kinds.cpp
for b in rt_main trap_main; do
    n=$(nm -u "${WORK}/${b}" | grep -c '__ubsan_handle_' || true)
    lib=$(otool -L "${WORK}/${b}" | grep -o 'libclang_rt[^ ]*' | sed 's,.*/,,' || true)
    echo "${b}: undefined __ubsan_handle_* symbols: ${n}; UBSan dylib: ${lib:-none}"
done
show "runtime build" rt_main ""
show "trap build" trap_main ""

# -g plus basic trap reasons make the compiler name each trap in DWARF, so the ordinal
# below is cross-checked against what this compiler itself says the trap is for.
"${CXX}" -O2 -g -c -fsanitize=signed-integer-overflow,integer-divide-by-zero,bounds \
    -fsanitize-trap=all -fsanitize-debug-trap-reasons=basic trap_kinds.cpp -o "${WORK}/kinds.o"
# Clang's SanitizerHandler enum (clang/lib/CodeGen/SanitizerHandler.h); the trap
# immediate is 0x5500 | ordinal on AArch64.
KINDS=(add_overflow builtin_unreachable cfi_check_fail divrem_overflow
       dynamic_type_cache_miss float_cast_overflow function_type_mismatch
       implicit_conversion invalid_builtin invalid_objc_cast load_invalid_value
       missing_return mul_overflow negate_overflow nullability_arg
       nullability_return nonnull_arg nonnull_return out_of_bounds
       pointer_overflow shift_out_of_bounds sub_overflow type_mismatch)
# The reason text the compiler is expected to attach to each function's trap.
declare -A WANT=([add]="Integer addition overflowed" [mul]="Integer multiplication overflowed"
                 [sdiv]="Integer divide or remainder overflowed" [index4]="Array index out of bounds")
for fn in add mul sdiv index4; do
    while read -r addr imm; do
        reason=$(dwarfdump --lookup="0x${addr}" "${WORK}/kinds.o" | grep -oE '__clang_trap_msg[^"]*' | sed 's/.*\$//')
        v=$((16#${imm#\#0x}))
        hi=$((v >> 8))
        ch=$(awk -v n="${hi}" 'BEGIN { printf "%c", n }')
        printf '%-7s brk %s  high byte 0x%02x (%s)  ordinal %2d -> %s; compiler reason: %s\n' \
            "${fn}" "${imm}" "${hi}" "${ch}" $((v & 0xff)) "${KINDS[$((v & 0xff))]}" "${reason}"
        if [ "${reason}" != "${WANT[${fn}]}" ]; then
            echo "FAIL: ${fn} trap reason is '${reason}', expected '${WANT[${fn}]}'" >&2
            exit 1
        fi
    done < <(objdump -d --no-show-raw-insn --disassemble-symbols="_${fn}" "${WORK}/kinds.o" \
        | awk '/brk/ { sub(":", "", $1); print $1, $3 }' | sort -u)
done

for mode in "none" "trap" "runtime"; do
    case "${mode}" in
        none) flags=() ;;
        trap) flags=(-fsanitize=signed-integer-overflow -fsanitize-trap=signed-integer-overflow) ;;
        runtime) flags=(-fsanitize=signed-integer-overflow) ;;
    esac
    "${CXX}" -O2 -c "${flags[@]+"${flags[@]}"}" trap_kinds.cpp -o "${WORK}/size.o"
    n=$(objdump -d --no-show-raw-insn --disassemble-symbols=_add "${WORK}/size.o" | grep -cE '^[[:space:]]*[0-9a-f]+:' || true)
    echo "add -O2 instrumentation ${mode}: ${n} instructions"
done

for f in "" "-fno-sanitize-merge=signed-integer-overflow"; do
    "${CXX}" -O2 -c -fsanitize=signed-integer-overflow -fsanitize-trap=signed-integer-overflow \
        ${f} trap_kinds.cpp -o "${WORK}/merge.o"
    n=$(objdump -d --no-show-raw-insn --disassemble-symbols=_two_adds_one_mul "${WORK}/merge.o" | grep -c 'brk' || true)
    echo "two_adds_one_mul -O2 ${f:-(merge on)}: trap instructions: ${n}"
done

# For each trap: the line-table line, and the call line of the artificial trap-reason frame.
for f in "" "-fno-sanitize-merge=signed-integer-overflow" \
         "-fno-sanitize-debug-trap-reasons" \
         "-fno-sanitize-merge=signed-integer-overflow -fno-sanitize-debug-trap-reasons"; do
    # shellcheck disable=SC2086
    "${CXX}" -O2 -g -c -fsanitize=signed-integer-overflow -fsanitize-trap=signed-integer-overflow \
        ${f} merge_lines.cpp -o "${WORK}/lines.o"
    echo "three_adds -O2 ${f:-(defaults)}:"
    objdump -d --no-show-raw-insn "${WORK}/lines.o" | awk '/brk/ { sub(":", "", $1); print $1 }' \
        | while read -r addr; do
            info=$(dwarfdump --lookup="0x${addr}" "${WORK}/lines.o")
            line=$(grep -oE 'Line info: [^,]+, line [0-9]+' <<<"${info}" | grep -oE '[0-9]+$')
            call=$(grep -oE 'DW_AT_call_line[[:space:]]+\([0-9]+\)' <<<"${info}" | grep -oE '[0-9]+' || true)
            echo "  trap: line table line ${line}; trap-reason frame call line ${call:-none}"
        done
done

echo
echo "== 8. Trap reasons recorded in DWARF (fake inlined frame names)"
for f in "-O0" "-O2" "-O2 -fsanitize-debug-trap-reasons=basic" "-O2 -fno-sanitize-debug-trap-reasons"; do
    # shellcheck disable=SC2086
    "${CXX}" -g -c ${f} -fsanitize=signed-integer-overflow -fsanitize-trap=signed-integer-overflow \
        trap_kinds.cpp -o "${WORK}/reasons.o"
    echo "${f}:"
    dwarfdump --debug-info "${WORK}/reasons.o" | grep -oE '__clang_trap_msg[^"]*' | sort -u \
        | sed 's/^/  /' | grep . || echo "  (no trap reasons)"
done

echo
echo "== 9. The optimizer assumes no signed overflow: x + 1 > x at INT_MAX"
build opt_plain -O2 optimizer.cpp
build opt_wrapv -O2 -fwrapv optimizer.cpp
build opt_ubsan -O2 -fsanitize=signed-integer-overflow optimizer.cpp
show "-O2" opt_plain ""
show "-O2 -fwrapv" opt_wrapv ""
show "-O2 -fsanitize=signed-integer-overflow" opt_ubsan ""
body=$(objdump -d --no-show-raw-insn --disassemble-symbols=__Z19plus_one_is_greateri "${WORK}/opt_plain" \
    | awk '/^[[:space:]]*[0-9a-f]+:/{ sub(/;.*/, ""); $1=""; sub(/^ +/, ""); sub(/ +$/, ""); printf "%s; ", $0 }')
echo "-O2 body of plus_one_is_greater: ${body}"

echo
echo "== 10. Minimal runtime availability in this toolchain"
if "${CXX}" -O1 -fsanitize=undefined -fsanitize-minimal-runtime trap_main.cpp trap_kinds.cpp \
        -o "${WORK}/minimal" >"${WORK}/min.log" 2>&1; then
    echo "-fsanitize-minimal-runtime: linked"
    show "minimal runtime" minimal ""
else
    missing=$(grep -oE 'libclang_rt\.ubsan_minimal[^ ]*\.dylib' "${WORK}/min.log" | sed 's,.*/,,' | sort -u | head -1)
    warned=$(grep -c "does not support '-fsanitize-minimal-runtime'" "${WORK}/min.log" || true)
    echo "-fsanitize-minimal-runtime: link failed; missing ${missing:-unknown}; driver warnings: ${warned}"
fi
