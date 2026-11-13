#!/usr/bin/env bash
# requires: macos
#
# Rust CPU profiling lab: what a frame-pointer unwinder sees in an optimized Rust binary,
# what debug info adds on top of it, and how rustc's frame-pointer flags change codegen.
# Needs cargo, plus the Xcode command-line tools (atos, dsymutil, dwarfdump, objdump, otool, c++filt).
set -euo pipefail

export LC_ALL=C
export CARGO_TERM_COLOR=never
unset RUSTFLAGS CARGO_ENCODED_RUSTFLAGS CARGO_BUILD_RUSTFLAGS

LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Build from a private copy so Cargo.lock and target/ never land in the lab directory.
cp -R "${LAB}/Cargo.toml" "${LAB}/src" "${WORK}/"
cd "${WORK}"

for tool in cargo rustc atos dsymutil dwarfdump objdump otool c++filt; do
    command -v "${tool}" >/dev/null || { echo "missing tool: ${tool}" >&2; exit 1; }
done

host="$(rustc -vV | awk '/^host:/ {print $2}')"
minor="$(rustc -vV | awk '/^release:/ {split($2, v, "."); print v[2]}')"
[ "${host}" = "aarch64-apple-darwin" ] || { echo "this lab is recorded on aarch64-apple-darwin, host is ${host}" >&2; exit 1; }

# build <profile> <target-dir> [rustflags]: build quietly, print nothing.
build() {
    RUSTFLAGS="${3:-}" CARGO_TARGET_DIR="${WORK}/$2" cargo build --profile "$1" -q
}

# flags_for <profile>: the debuginfo, strip, and split-debuginfo flags cargo hands rustc for this crate.
flags_for() {
    CARGO_TARGET_DIR="${WORK}/flags-$1" cargo build --profile "$1" -v 2>&1 \
        | grep -e '--crate-name rust_flamegraph' \
        | grep -o -e '-C opt-level=[0-9sz]*' -e '-C debuginfo=[a-z0-9-]*' -e '-C strip=[a-z]*' -e '-C split-debuginfo=[a-z]*' \
        | tr '\n' ' ' | sed 's/ $//'
}

echo "== 1. What cargo passes to rustc for this crate"
echo "host: ${host}"
echo "release:        $(flags_for release)"
echo "profiling:      $(flags_for profiling)"
echo "profiling-full: $(flags_for profiling-full)"
echo

build release rel
build profiling prof
build profiling-full full
REL="${WORK}/rel/release/rust_flamegraph"
PROF="${WORK}/prof/profiling/rust_flamegraph"
FULL="${WORK}/full/profiling-full/rust_flamegraph"

text_hash() { otool -t "$1" | tail -n +2 | shasum | cut -c1-16; }
text_size() { size -m "$1" | awk '/Section __text:/ {print $3}'; }

echo "== 2. Debug info does not change the machine code"
printf '%-15s __text bytes: %s\n' "release" "$(text_size "${REL}")"
printf '%-15s __text bytes: %s\n' "profiling" "$(text_size "${PROF}")"
printf '%-15s __text bytes: %s\n' "profiling-full" "$(text_size "${FULL}")"
if [ "$(text_hash "${REL}")" = "$(text_hash "${PROF}")" ] && [ "$(text_hash "${REL}")" = "$(text_hash "${FULL}")" ]; then
    echo "__text identical across release, profiling, profiling-full: yes"
else
    echo "__text identical across release, profiling, profiling-full: NO"
fi
echo

echo "== 3. Debug info that reaches a symbolizer"
printf '%-15s %10s %12s %10s %10s\n' "profile" "OSO-refs" "inlined-DIEs" "variables" "subprogs"
for pair in "release:${REL}" "profiling:${PROF}" "profiling-full:${FULL}"; do
    name="${pair%%:*}"
    bin="${pair#*:}"
    oso="$(dsymutil -s "${bin}" | grep -c 'N_OSO' || true)"
    dsymutil "${bin}" -o "${bin}.dSYM" 2>/dev/null || true
    if [ -d "${bin}.dSYM" ]; then
        dwarfdump --debug-info "${bin}.dSYM" > "${bin}.dies" 2>/dev/null || true
    else
        : > "${bin}.dies"
    fi
    inl="$(grep -c 'DW_TAG_inlined_subroutine' "${bin}.dies" || true)"
    var="$(grep -c 'DW_TAG_variable' "${bin}.dies" || true)"
    sub="$(grep -c 'DW_TAG_subprogram' "${bin}.dies" || true)"
    printf '%-15s %10s %12s %10s %10s\n' "${name}" "${oso}" "${inl}" "${var}" "${sub}"
done
echo

# walk <binary> <symbol-file> <label>: run the frame-pointer walk and symbolize it two ways.
walk() {
    local bin="$1" syms="$2" out load addrs
    out="$("${bin}" walk)"
    load="$(printf '%s\n' "${out}" | awk '/^LOAD/ {print $2}')"
    addrs="$(printf '%s\n' "${out}" | awk '/^RET/ {print $2}' | tr '\n' ' ')"
    echo "physical frames walked through x29: $(printf '%s\n' "${out}" | grep -c '^RET')"
    echo "-- atos, one name per return address:"
    # shellcheck disable=SC2086
    atos -o "${syms}" -l "${load}" ${addrs} | c++filt | sed -e 's/ (in rust_flamegraph)//' \
        -e 's/^0x[0-9a-f]*$/?? (outside the executable: dyld start)/' -e 's/^/   /'
    echo "-- atos -i, inline frames expanded from DWARF:"
    # shellcheck disable=SC2086
    atos -o "${syms}" -l "${load}" -i ${addrs} | c++filt | grep -v '^$' | sed -e 's/ (in rust_flamegraph)//' \
        -e 's/^0x[0-9a-f]*$/?? (outside the executable: dyld start)/' -e 's/^/   /'
    echo "-- std::backtrace (libunwind for the walk, gimli for names):"
    printf '%s\n' "${out}" | awk '/^BT / {sub(/^BT /, ""); print "   " $0}'
    printf '%s\n' "${out}" | grep -e '^SUM' -e '^MIX' | sed 's/^/   /'
}

echo "== 4. release (no debug info): the walk and its names"
walk "${REL}" "${REL}"
echo
echo "== 5. profiling (line-tables-only): the same walk with inline scopes"
walk "${PROF}" "${PROF}.dSYM/Contents/Resources/DWARF/rust_flamegraph"
echo

# fp_row <label> <target-dir>: does each lab function save x29/x30, and how long is the leaf?
fp_row() {
    local dis leaf nonleaf
    dis="$(objdump -d --no-show-raw-insn --disassemble-symbols=_lab_leaf_mix,_lab_nonleaf_mix "${WORK}/$2/release/rust_flamegraph")"
    leaf="$(printf '%s\n' "${dis}" | awk '/<_lab_leaf_mix>:/ {on=1; next} /<_lab_nonleaf_mix>:/ {on=0} on && NF' )"
    nonleaf="$(printf '%s\n' "${dis}" | awk '/<_lab_nonleaf_mix>:/ {on=1; next} on && NF')"
    has() { if printf '%s\n' "$1" | grep -q 'stp[[:space:]]*x29, x30'; then echo yes; else echo no; fi; }
    printf '%-30s %-12s %-15s %s\n' "$1" "$(has "${leaf}")" "$(has "${nonleaf}")" "$(printf '%s\n' "${leaf}" | grep -c .)"
}

echo "== 6. Frame-pointer flags on ${host} (release profile)"
build release fp-default
build release fp-yes "-Cforce-frame-pointers=yes"
build release fp-no "-Cforce-frame-pointers=no"
printf '%-30s %-12s %-15s %s\n' "rustflags" "leaf saves" "non-leaf saves" "leaf instructions"
fp_row "(none: target says non-leaf)" fp-default
fp_row "-Cforce-frame-pointers=yes" fp-yes
fp_row "-Cforce-frame-pointers=no" fp-no
printf 'pub fn f() {}\n' > probe.rs
for v in non-leaf always; do
    if err="$(rustc --crate-type lib "-Cforce-frame-pointers=${v}" --out-dir "${WORK}" probe.rs 2>&1)"; then
        echo "stable rustc accepts -Cforce-frame-pointers=${v}: yes"
    else
        echo "stable rustc accepts -Cforce-frame-pointers=${v}: no ($(printf '%s\n' "${err}" | head -n 1))"
    fi
done
echo

echo "== 7. Symbol mangling"
sym="$(nm -j "${REL}" | grep 'hot_path' | head -n 1)"
case "${sym}" in
    __R*) echo "hot_path is mangled with v0 (Mach-O symbol starts with __R): yes" ;;
    *) echo "hot_path is mangled with v0 (Mach-O symbol starts with __R): no" ;;
esac
echo "c++filt on the symbol: $(printf '%s\n' "${sym#_}" | c++filt)"
if [ "${minor}" -ge 97 ]; then
    echo "rustc is 1.97 or later, where v0 is the stable default: yes"
else
    echo "rustc is 1.97 or later, where v0 is the stable default: no"
fi
