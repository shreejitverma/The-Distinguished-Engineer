#!/usr/bin/env bash
# requires: linux
set -euo pipefail

# The relocation names, -mtls-dialect=trad, and the expected output are AArch64-specific.
case "$(uname -m)" in
    aarch64|arm64) ;;
    *) echo "tls_models: this lab records AArch64 relocations; run it on linux-aarch64 (got $(uname -m))" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
cd "${WORK_DIR}"

CC="${CC:-gcc}"

# 1. Compile relocatable object files under traditional and TLSDESC dialects
"${CC}" -O2 -fPIC -c "${SCRIPT_DIR}/models.c" -o models_trad.o -mtls-dialect=trad
"${CC}" -O2 -fPIC -c "${SCRIPT_DIR}/models.c" -o models_desc.o -mtls-dialect=desc

echo "=== 1. Relocations in Object Files ==="
echo "Traditional dialect relocations:"
readelf -r -W models_trad.o | grep -E "TLS|CALL26" | awk '{print $3, $5}' | sort -u
echo "TLSDESC dialect relocations:"
readelf -r -W models_desc.o | grep -E "TLS|CALL26|__tls_get_addr" | awk '{print $3, $5}' | sort -u
echo "R_AARCH64_TLSLD relocations (trad, desc): $(readelf -r -W models_trad.o | grep -c TLSLD || true) $(readelf -r -W models_desc.o | grep -c TLSLD || true)"
echo "__tls_get_addr symbol references (trad, desc): $(readelf -s -W models_trad.o | grep -c __tls_get_addr || true) $(readelf -s -W models_desc.o | grep -c __tls_get_addr || true)"
echo "blr instructions in get_ld (trad, desc): $(objdump -d models_trad.o --disassemble=get_ld | grep -cE '[[:space:]]blr[[:space:]]' || true) $(objdump -d models_desc.o --disassemble=get_ld | grep -cE '[[:space:]]blr[[:space:]]' || true)"

echo "=== 2. Relocations in Shared Libraries ==="
# Local Exec is invalid in a shared object, so the shared builds leave var_le out.
"${CC}" -O2 -fPIC -DNO_LE -c "${SCRIPT_DIR}/models.c" -o models_trad_so.o -mtls-dialect=trad
"${CC}" -O2 -fPIC -DNO_LE -c "${SCRIPT_DIR}/models.c" -o models_desc_so.o -mtls-dialect=desc
"${CC}" -shared -o libmodels_trad.so models_trad_so.o
"${CC}" -shared -o libmodels_desc.so models_desc_so.o
echo "var_le (Local Exec) symbols in the shared objects (trad, desc): $(nm libmodels_trad.so | grep -c var_le || true) $(nm libmodels_desc.so | grep -c var_le || true)"
echo "libmodels_trad.so dynamic TLS relocations:"
readelf -r -W libmodels_trad.so | grep -E "TLS|__tls_get_addr" | awk '{print $3, $5}' | sort -u
echo "libmodels_desc.so dynamic TLS relocations:"
readelf -r -W libmodels_desc.so | grep -E "TLS|__tls_get_addr" | awk '{print $3, $5}' | sort -u
echo "__tls_get_addr dynamic symbol references (trad, desc): $(readelf --dyn-syms -W libmodels_trad.so | grep -c __tls_get_addr || true) $(readelf --dyn-syms -W libmodels_desc.so | grep -c __tls_get_addr || true)"

echo "=== 3. Static Linker Relaxation ==="
"${CC}" -O2 models_desc.o "${SCRIPT_DIR}/main.c" -o prog_desc
echo "prog_desc dynamic TLS relocations (post-link relaxation):"
readelf -r -W prog_desc | grep -E "TLS|__tls_get_addr" || echo "none (fully relaxed to Local Exec)"
count_insn() { # count_insn <insn> <file> <function>
    objdump -d "$2" --disassemble="$3" | grep -cE "[[:space:]]$1[[:space:]]|[[:space:]]$1\$" || true
}
echo "get_ld instructions blr/movz/movk/nop: models_desc.o $(count_insn blr models_desc.o get_ld)/$(count_insn movz models_desc.o get_ld)/$(count_insn movk models_desc.o get_ld)/$(count_insn nop models_desc.o get_ld)," \
     "prog_desc $(count_insn blr prog_desc get_ld)/$(count_insn movz prog_desc get_ld)/$(count_insn movk prog_desc get_ld)/$(count_insn nop prog_desc get_ld)"
./prog_desc

echo "=== 4. Thread Isolation Across POSIX Threads ==="
"${CC}" -O2 models_desc.o "${SCRIPT_DIR}/threads.c" -o prog_threads -lpthread
./prog_threads

echo "=== 5. PT_TLS Segment Inspection ==="
readelf -l -W prog_desc | grep "TLS" | python3 -c '
import sys
line = sys.stdin.readline().strip()
parts = line.split()
filesz = int(parts[4], 16)
memsz = int(parts[5], 16)
print(f"PT_TLS filesz={filesz} bytes memsz={memsz} bytes")
'

echo "=== 6. Static TLS Surplus Exhaustion & Remediation ==="
# make_tls_lib <name> <bytes> [tls_model]: a shared library with one TLS buffer of that size.
make_tls_lib() {
    local model=""
    if [ -n "${3:-}" ]; then model="__attribute__((tls_model(\"$3\")))"; fi
    printf '__thread char buffer[%s] %s;\nint access_buffer(int idx) { return buffer[idx]; }\n' "$2" "${model}" > "$1.c"
    "${CC}" -O2 -fPIC -shared "$1.c" -o "$1.so"
}
make_tls_lib liblarge_ie $((96 * 1024)) initial-exec
"${CC}" -O2 "${SCRIPT_DIR}/dlopen_test.c" -o dlopen_test

echo "Default dlopen with 96 KiB Initial Exec library:"
./dlopen_test ./liblarge_ie.so || true

echo "Remediation 1: Increase surplus with GLIBC_TUNABLES optional_static_tls:"
GLIBC_TUNABLES="glibc.rtld.optional_static_tls=200000" ./dlopen_test ./liblarge_ie.so

echo "Remediation 2: Recompile library with Global Dynamic (GD) model:"
make_tls_lib liblarge_gd $((96 * 1024))
./dlopen_test ./liblarge_gd.so

echo "Remediation 3: Load the library at startup with LD_PRELOAD:"
LD_PRELOAD=./liblarge_ie.so ./dlopen_test ./liblarge_ie.so

echo "Default surplus threshold with Initial Exec libraries (glibc 2.39):"
make_tls_lib libie_1664 1664 initial-exec
make_tls_lib libie_1700 1700 initial-exec
./dlopen_test ./libie_1664.so || true
./dlopen_test ./libie_1700.so || true
