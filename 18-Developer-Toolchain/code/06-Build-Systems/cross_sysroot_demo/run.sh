#!/usr/bin/env bash
# Cross-compilation, triples, and hermetic sysroots.
# Shows what a target triple fixes in the ABI, how the host leaks into a cross build, and what
# --sysroot does and does not isolate in gcc and clang, at compile, link, and run time.
# requires: linux
# Needs a Debian or Ubuntu multiarch host with gcc, clang, binutils, libc6-dev, and python3,
# which the harness lab image provides. No network, no root, no QEMU.
set -euo pipefail
export LC_ALL=C PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${HERE}/src"
# The lab contrasts two compiler drivers, so it names both instead of using ${CC:-cc}.
GCC="${GCC:-gcc}"
CLANG="${CLANG:-clang}"

die() { echo "preflight: $*" >&2; exit 2; }
for tool in "${GCC}" "${CLANG}" readelf python3; do
    command -v "${tool}" >/dev/null 2>&1 || die "missing ${tool}"
done
MA="$("${GCC}" -print-multiarch)"
[ -n "${MA}" ] || die "gcc -print-multiarch is empty; this lab needs a Debian-style multiarch host"

W="$(mktemp -d)"
trap 'rm -rf "${W}"' EXIT
S="${W}/sysroot"
cd "${W}"

# Replace run-specific paths with stable names; compiler temp objects become <TMP>.o.
scrub() {
    sed -e "s#${S}#<SYSROOT>#g" -e "s#${W}#<WORK>#g" -e 's#/tmp/[A-Za-z0-9_.-]*\.o#<TMP>.o#g'
}

echo "== 1. What the triple decides (clang $("${CLANG}" -dumpversion), no sysroot, freestanding objects)"
printf '%-24s %-32s %-4s %-3s %-4s %-8s %-23s %-18s %s\n' \
    "--target" "effective triple" "long" "ptr" "ldbl" "char" "ELF header" "ld -m" "-dynamic-linker"
for t in x86_64-linux-gnu i686-linux-gnu aarch64-linux-gnu armv7a-linux-gnueabihf \
         riscv64-linux-gnu powerpc64le-linux-gnu powerpc64-linux-gnu s390x-linux-gnu; do
    eff="$("${CLANG}" --target="${t}" -print-effective-triple)"
    macros="$("${CLANG}" --target="${t}" -ffreestanding -nostdinc -E -dM -x c /dev/null)"
    m() { printf '%s\n' "${macros}" | awk -v k="$1" '$2 == k { print $3 }'; }
    if printf '%s\n' "${macros}" | grep -q '__CHAR_UNSIGNED__'; then sign=unsigned; else sign=signed; fi
    "${CLANG}" --target="${t}" -ffreestanding -nostdinc -c "${SRC}/probe.c" -o "probe.${t}.o"
    ident="$(python3 -I "${SRC}/elf_ident.py" "probe.${t}.o")"
    # -### prints the driver's link command without running it: the triple picks emulation and loader.
    link="$("${CLANG}" --target="${t}" -### "${SRC}/probe.c" -o probe 2>&1 | tail -n 1)"
    emu="$(printf '%s\n' "${link}" | tr ' ' '\n' | tr -d '"' | grep -A1 -x -- '-m' | tail -n 1)"
    interp="$(printf '%s\n' "${link}" | tr ' ' '\n' | tr -d '"' | grep -A1 -x -- '-dynamic-linker' | tail -n 1)"
    printf '%-24s %-32s %-4s %-3s %-4s %-8s %-23s %-18s %s\n' "${t}" "${eff}" \
        "$(m __SIZEOF_LONG__)" "$(m __SIZEOF_POINTER__)" "$(m __SIZEOF_LONG_DOUBLE__)" "${sign}" \
        "${ident}" "${emu}" "${interp}"
done

echo
echo "== 2. Cross without a sysroot: the host's files fill the gaps (--target=x86_64-linux-gnu on $("${GCC}" -dumpmachine))"
echo "-- #include <...> search list:"
"${CLANG}" --target=x86_64-linux-gnu -fsyntax-only -v -x c /dev/null 2>&1 \
    | sed -n '/<\.\.\.> search starts here/,/End of search list/p' | sed '1d;$d'
printf '#include <stdio.h>\n' > needs_stdio.c
echo "-- compile a file that includes <stdio.h>:"
if "${CLANG}" --target=x86_64-linux-gnu -fsyntax-only needs_stdio.c > compile.log 2>&1; then
    echo "compiled (unexpected)"
else
    grep -m1 'error' compile.log | scrub
fi
echo "-- link a freestanding object for x86_64 with the host's binutils:"
if "${CLANG}" --target=x86_64-linux-gnu -ffreestanding -nostdlib "${SRC}/probe.c" -o probe.x86_64 > link.log 2>&1; then
    echo "linked (unexpected)"
else
    head -n 1 link.log
fi

echo
echo "== 3. One sysroot, many triples: the driver picks <sysroot>/usr/include/<multiarch tuple>"
X="${W}/multi"
for tuple in x86_64-linux-gnu i386-linux-gnu aarch64-linux-gnu arm-linux-gnueabihf riscv64-linux-gnu; do
    mkdir -p "${X}/usr/include/${tuple}"
    printf '#define TARGET_ABI_DIR "%s"\n' "${tuple}" > "${X}/usr/include/${tuple}/target_abi.h"
done
for t in x86_64-linux-gnu i686-linux-gnu aarch64-linux-gnu armv7a-linux-gnueabihf riscv64-linux-gnu powerpc64le-linux-gnu; do
    if out="$("${CLANG}" --target="${t}" --sysroot="${X}" -E -P "${SRC}/which_multiarch.c" 2>&1)"; then
        picked="$(printf '%s\n' "${out}" | sed -n 's/^const char \*picked = "\(.*\)";$/\1/p')"
        printf '%-24s -> usr/include/%s\n' "${t}" "${picked}"
    else
        printf '%-24s -> %s\n' "${t}" "$(printf '%s\n' "${out}" | grep -m1 'error' | sed 's#^.*error: ##')"
    fi
done

echo
echo "== 4. Build a sysroot from this host's own glibc (${MA}); a real one comes from mmdebstrap or a vendor SDK"
# The program interpreter path the host's binaries carry, for example /lib/ld-linux-aarch64.so.1.
printf 'int main(void) { return 0; }\n' > host.c
"${GCC}" host.c -o host
LDSO="$(readelf -lW host | sed -n 's/.*Requesting program interpreter: \(.*\)]$/\1/p')"
mkdir -p "${S}/usr/include" "${S}/usr/lib/${MA}" "${S}/lib/${MA}" "${S}/usr/lib/gcc/${MA}" "${S}$(dirname "${LDSO}")"
cp -R /usr/include/. "${S}/usr/include/"
for f in Scrt1.o crt1.o crti.o crtn.o libc.so libc_nonshared.a; do cp "/usr/lib/${MA}/${f}" "${S}/usr/lib/${MA}/"; done
cp -L "/lib/${MA}/libc.so.6" "/lib/${MA}/libgcc_s.so.1" "${S}/lib/${MA}/"
cp -L "${LDSO}" "${S}${LDSO}"
GCCVER="$("${GCC}" -dumpversion)"
cp -R "/usr/lib/gcc/${MA}/${GCCVER%%.*}" "${S}/usr/lib/gcc/${MA}/"
# A dependency that is installed only into the sysroot, as a staging prefix would install it.
cp "${SRC}/foo.h" "${S}/usr/include/"
"${CLANG}" --sysroot="${S}" -fPIC -shared -Wl,-soname,libfoo.so.1 "${SRC}/foo.c" -o "${S}/usr/lib/${MA}/libfoo.so.1"
ln -s libfoo.so.1 "${S}/usr/lib/${MA}/libfoo.so"
echo "program interpreter: ${LDSO}"
echo "headers copied: $(find "${S}/usr/include" -type f | wc -l | tr -d ' ')"
echo "libc.so is a linker script with absolute paths:"
grep '^GROUP' "${S}/usr/lib/${MA}/libc.so"

classify() {
    # Count the files the linker opened inside and outside the sysroot.
    local inside outside
    inside="$(grep -c "^${S}/" "$1" || true)"
    outside="$(grep -v "^${S}/" "$1" | grep -vc '^/tmp/' || true)"
    echo "files opened: ${inside} inside the sysroot, ${outside} outside it"
}

echo
echo "== 5. gcc --sysroot (a native gcc built without --with-sysroot)"
"${GCC}" --sysroot="${S}" -M "${SRC}/app.c" | tr ' ' '\n' | grep '^/' \
    | sed -E -e "s#^${S}/usr/include/.*#<SYSROOT>/usr/include#" -e 's#^(/usr/lib/gcc/[^/]+/[0-9]+/include)/.*#\1#' \
    | grep -v "^${SRC}" | sort | uniq -c | sed 's/^ */headers: /'
"${GCC}" --sysroot="${S}" "${SRC}/app.c" -lfoo -o app.gcc -Wl,--trace > gcc.trace 2>&1
echo "-- ld --trace:"
scrub < gcc.trace
classify gcc.trace

echo
echo "== 6. clang --sysroot with a complete sysroot (libgcc and crt files inside it)"
"${CLANG}" --sysroot="${S}" "${SRC}/app.c" -lfoo -o app -Wl,--trace > clang.trace 2>&1
echo "-- ld --trace:"
scrub < clang.trace
classify clang.trace
echo "-- library search path the driver passed:"
"${CLANG}" --sysroot="${S}" -### "${SRC}/app.c" -lfoo 2>&1 | tail -n 1 | tr ' ' '\n' | tr -d '"' | grep '^-L' | scrub

echo
echo "== 7. Prove it: delete libc.so from the sysroot and link again"
mv "${S}/usr/lib/${MA}/libc.so" "${W}/libc.so.saved"
if "${CLANG}" --sysroot="${S}" "${SRC}/app.c" -lfoo -o app.nolibc > nolibc.log 2>&1; then
    echo "clang: linked (the sysroot is not hermetic)"
else
    echo "clang: $(grep -m1 'cannot find' nolibc.log | sed 's#^.*ld: ##')"
fi
if "${GCC}" --sysroot="${S}" "${SRC}/app.c" -lfoo -o app.nolibc.gcc > nolibc.gcc.log 2>&1; then
    echo "gcc:   linked anyway, against the host's libc.so"
else
    echo "gcc:   $(grep -m1 'cannot find' nolibc.gcc.log | sed 's#^.*ld: ##')"
fi
mv "${W}/libc.so.saved" "${S}/usr/lib/${MA}/libc.so"

echo
echo "== 8. What the binary carries to the target"
echo "PT_INTERP: $(readelf -lW app | sed -n 's/.*Requesting program interpreter: \(.*\)]$/\1/p')"
readelf -dW app | sed -n 's/.*(NEEDED).*\[\(.*\)\]$/DT_NEEDED: \1/p'
echo "RUNPATH entries: $(readelf -dW app | grep -c -E '\((RUNPATH|RPATH)\)' || true)"
echo "versioned imports (the sysroot's glibc sets this floor):"
readelf --dyn-syms -W app | awk '$7 == "UND" && $8 ~ /@/ { sub(/ .*/, "", $8); print "  " $8 }' | sort

echo
echo "== 9. Run time: the loader knows nothing about the sysroot"
echo "-- ./app"
./app > run.log 2>&1 || echo "exit $?: $(sed 's#^.*: error#error#' run.log)"
echo "-- sysroot loader with --library-path (what qemu-user -L does for a foreign binary)"
LD_DEBUG=libs "${S}${LDSO}" --library-path "${S}/lib/${MA}:${S}/usr/lib/${MA}" ./app 2> ld_debug.log
grep 'calling init:' ld_debug.log | sed 's/^[[:space:]]*[0-9]*:[[:space:]]*//' | scrub | sort
echo "-- link with -rpath pointing into the sysroot"
"${CLANG}" --sysroot="${S}" "${SRC}/app.c" -lfoo -o app.rpath -Wl,-rpath,"${S}/usr/lib/${MA}"
readelf -dW app.rpath | sed -n 's/.*(RUNPATH).*\[\(.*\)\]$/RUNPATH: \1/p' | scrub
./app.rpath
echo "-- ship the dependency next to the binary with \$ORIGIN"
mkdir -p dist/bin dist/lib
cp -L "${S}/usr/lib/${MA}/libfoo.so.1" dist/lib/
"${CLANG}" --sysroot="${S}" "${SRC}/app.c" -lfoo -o dist/bin/app -Wl,-rpath,'$ORIGIN/../lib'
readelf -dW dist/bin/app | sed -n 's/.*(RUNPATH).*\[\(.*\)\]$/RUNPATH: \1/p'
dist/bin/app
