#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"

CC="${CC:-gcc}"

# 1. Compile the ELF section and segment parser
"${CC}" -O2 -Wall -Wextra -Werror -pedantic "${SCRIPT_DIR}/elf_parser.c" -o elf_parser

# 2. Create and compile a reference C program with distinct data, bss, rodata, and text sections
cat << 'EOF' > target.c
#include <stdio.h>

const char rodata_string[] = "Toolchain Zero to Godhood ELF Parser Verification";
int initialized_global_counter = 42;
int uninitialized_bss_buffer[256];

int calculate(int x) {
    return x * 2 + initialized_global_counter;
}

int main(void) {
    uninitialized_bss_buffer[0] = calculate(5);
    return uninitialized_bss_buffer[0] == 52 ? 0 : 1;
}
EOF

"${CC}" -O2 -Wl,--build-id=none target.c -o target

echo "=== 1. ELF Section and Segment Parser Output ==="
./elf_parser target

echo ""
echo "=== 2. Cross-Verification with GNU readelf ==="
# Extract section to segment mapping from readelf -W -l
readelf -W -l target | sed -n '/Section to Segment mapping:/,/^$/p' > readelf_mapping.txt

# Extract section to segment mapping from our parser
./elf_parser target | sed -n '/Section to Segment Mapping:/,/Allocated vs Unallocated/p' | grep -E '^   [0-9]{2}' > parser_mapping.txt
readelf -W -l target | sed -n '/Section to Segment mapping:/,/^$/p' | grep -E '^   [0-9]{2}' > readelf_filtered.txt

if diff -u parser_mapping.txt readelf_filtered.txt > /dev/null; then
    echo "Verification: Parser Section-to-Segment mapping matches GNU readelf 100% exactly (PASS)"
else
    echo "Verification: Parser Section-to-Segment mapping MISMATCH (FAIL)"
    diff -u parser_mapping.txt readelf_filtered.txt
    exit 1
fi

echo ""
echo "=== 3. Invariant Enforcement & Failure Mode Testing ==="

# Test A: Corrupt Magic Bytes
cp target target_bad_magic
python3 -c '
with open("target_bad_magic", "r+b") as f:
    f.seek(0)
    f.write(b"\x7fBAD")
'
set +e
./elf_parser target_bad_magic --quiet
RC_MAGIC=$?
set -e
if [ "${RC_MAGIC}" -eq 1 ]; then
    echo "Test A (Bad Magic): Rejection with exit code 1 (PASS)"
else
    echo "Test A (Bad Magic): Unexpected exit code ${RC_MAGIC} (FAIL)"
    exit 1
fi

# Test B: Violate System V Congruence Invariant (p_vaddr % p_align == p_offset % p_align)
cp target target_bad_congruence
python3 -c '
import struct
with open("target_bad_congruence", "r+b") as f:
    # Read e_phoff and e_phnum from Elf64_Ehdr
    f.seek(32)
    e_phoff = struct.unpack("<Q", f.read(8))[0]
    f.seek(56)
    e_phnum = struct.unpack("<H", f.read(2))[0]
    # Find first PT_LOAD segment with p_align > 1 and mutate p_offset
    for i in range(e_phnum):
        off = e_phoff + i * 56
        f.seek(off)
        p_type = struct.unpack("<I", f.read(4))[0]
        if p_type == 1: # PT_LOAD
            # p_offset is at offset + 8 in Elf64_Phdr
            f.seek(off + 8)
            p_offset = struct.unpack("<Q", f.read(8))[0]
            # Mutate offset by +1 to break modulo congruence
            f.seek(off + 8)
            f.write(struct.pack("<Q", p_offset + 1))
            break
'
set +e
./elf_parser target_bad_congruence --quiet
RC_CONG=$?
set -e
if [ "${RC_CONG}" -eq 6 ]; then
    echo "Test B (Congruence Violation): Rejection with exit code 6 (PASS)"
else
    echo "Test B (Congruence Violation): Unexpected exit code ${RC_CONG} (FAIL)"
    exit 1
fi

# Test C: Truncated ELF file (< 64 bytes)
head -c 32 target > target_truncated
set +e
./elf_parser target_truncated --quiet
RC_TRUNC=$?
set -e
if [ "${RC_TRUNC}" -eq 7 ]; then
    echo "Test C (Truncated Header): Rejection with exit code 7 (PASS)"
else
    echo "Test C (Truncated Header): Unexpected exit code ${RC_TRUNC} (FAIL)"
    exit 1
fi

# Test D: Out-of-bounds Section Header String Table Index (e_shstrndx >= e_shnum)
cp target target_bad_shstrndx
python3 -c '
import struct
with open("target_bad_shstrndx", "r+b") as f:
    # e_shstrndx is at offset 62 (2 bytes) in Elf64_Ehdr
    f.seek(62)
    f.write(struct.pack("<H", 0x9999))
'
set +e
./elf_parser target_bad_shstrndx --quiet
RC_SHSTR=$?
set -e
if [ "${RC_SHSTR}" -eq 5 ]; then
    echo "Test D (Bad e_shstrndx): Rejection with exit code 5 (PASS)"
else
    echo "Test D (Bad e_shstrndx): Unexpected exit code ${RC_SHSTR} (FAIL)"
    exit 1
fi

# Tests E-N mutate one field of a good binary with mutate.py and check the exact parser verdict.
cat << 'EOF' > mutate.py
import struct
import sys

src, dst, op = sys.argv[1:4]
d = bytearray(open(src, "rb").read())
e_phoff, e_shoff = struct.unpack_from("<QQ", d, 32)
e_phnum, e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHHH", d, 56)
SH0 = e_shoff
SH_TEXT = e_shoff + 12 * 64  # section [12] is .text in the reference binary


def nth_load(n):
    for i in range(e_phnum):
        off = e_phoff + i * 56
        if struct.unpack_from("<I", d, off)[0] == 1:
            if n == 0:
                return off
            n -= 1
    raise SystemExit("no such PT_LOAD")


if op == "xnum":            # e_phnum = PN_XNUM, real count parked in shdrs[0].sh_info
    struct.pack_into("<H", d, 56, 0xFFFF)
    struct.pack_into("<I", d, SH0 + 44, e_phnum)
elif op == "xnum_huge":     # same, but the parked count points far past the file
    struct.pack_into("<H", d, 56, 0xFFFF)
    struct.pack_into("<I", d, SH0 + 44, 0xFFFFFFFF)
elif op == "xindex":        # e_shstrndx = SHN_XINDEX, real index parked in shdrs[0].sh_link
    struct.pack_into("<H", d, 62, 0xFFFF)
    struct.pack_into("<I", d, SH0 + 40, e_shstrndx)
elif op == "xindex_bad":    # parked index is out of range
    struct.pack_into("<H", d, 62, 0xFFFF)
    struct.pack_into("<I", d, SH0 + 40, 99)
elif op == "reserved_shstrndx":  # inside SHN_LORESERVE..SHN_HIRESERVE, not SHN_XINDEX
    struct.pack_into("<H", d, 62, 0xFF00)
elif op == "xshnum":        # e_shnum = 0, real count parked in shdrs[0].sh_size
    struct.pack_into("<H", d, 60, 0)
    struct.pack_into("<Q", d, SH0 + 32, e_shnum)
elif op == "bad_shname":    # .text name offset beyond the name table
    struct.pack_into("<I", d, SH_TEXT, 0xFFFF)
elif op == "shstrtab_type":  # e_shstrndx names a PROGBITS section
    struct.pack_into("<H", d, 62, 1)
elif op == "align0":        # p_align = 0 on the RW PT_LOAD (vaddr != 0) means no alignment, no division by zero
    struct.pack_into("<Q", d, nth_load(1) + 48, 0)
elif op == "align_npot":    # p_align not a power of two
    struct.pack_into("<Q", d, nth_load(0) + 48, 0x3000)
elif op == "shstrtab_oob":  # the name table's file offset points past the end of the file
    struct.pack_into("<Q", d, e_shoff + e_shstrndx * 64 + 24, 0x7FFFFFF)
elif op == "empty_data":    # .data (section [21]) becomes a zero-size section
    struct.pack_into("<Q", d, e_shoff + 21 * 64 + 32, 0)
else:
    raise SystemExit("unknown op " + op)
open(dst, "wb").write(d)
EOF

# expect LABEL OP WANT_RC [WANT_TEXT]: mutate, run the full parser, and check exit code (and one output line).
expect() {
    local label="$1" op="$2" want="$3" text="${4:-}" rc=0 out
    python3 mutate.py target "target_${op}" "${op}"
    out="$(./elf_parser "target_${op}")" || rc=$?
    if [ "${rc}" -ne "${want}" ]; then
        echo "${label}: Unexpected exit code ${rc}, wanted ${want} (FAIL)"
        exit 1
    fi
    if [ -n "${text}" ] && ! grep -qF -- "${text}" <<< "${out}"; then
        echo "${label}: output lacks '${text}' (FAIL)"
        exit 1
    fi
    echo "${label}: exit code ${want} (PASS)"
}

expect "Test E (PN_XNUM phnum read from shdrs[0].sh_info)" xnum 0 "e_phnum:     9 program headers"
expect "Test F (PN_XNUM count beyond file)" xnum_huge 7
expect "Test G (SHN_XINDEX shstrndx read from shdrs[0].sh_link)" xindex 0 "e_shstrndx:  26 (string table index)"
expect "Test H (SHN_XINDEX target out of range)" xindex_bad 5
expect "Test I (reserved e_shstrndx 0xff00)" reserved_shstrndx 5
expect "Test J (e_shnum 0 read from shdrs[0].sh_size)" xshnum 0 "e_shnum:     27 section headers"
expect "Test K (sh_name outside the name table)" bad_shname 9
expect "Test L (e_shstrndx names a non-STRTAB section)" shstrtab_type 5
expect "Test M (p_align 0 accepted, no division by zero)" align0 0 "align=0x0 (congruence: vaddr%align=0 == offset%align=0 -> OK) [BSS"
expect "Test N (p_align not a power of two)" align_npot 6
expect "Test P (name table beyond file end)" shstrtab_oob 7

# Test O: a zero-size section inside a segment stays listed there, by the parser and by readelf alike.
python3 mutate.py target target_empty_data empty_data
readelf -W -l target_empty_data | sed -n '/Section to Segment mapping:/,/^$/p' | grep -E '^   [0-9]{2}' > readelf_empty.txt
./elf_parser target_empty_data | sed -n '/Section to Segment Mapping:/,/Allocated vs Unallocated/p' | grep -E '^   [0-9]{2}' > parser_empty.txt
if ./elf_parser target_empty_data | grep -qE '^  \[21\] \.data .* size=0x0 ' &&
   diff -u parser_empty.txt readelf_empty.txt > /dev/null && grep -q '\.data' parser_empty.txt; then
    echo "Test O (zero-size section kept in mapping, matches readelf): PASS"
else
    echo "Test O (zero-size section kept in mapping, matches readelf): MISMATCH (FAIL)"
    diff -u parser_empty.txt readelf_empty.txt
    exit 1
fi

echo ""
echo "All ELF structural invariants and failure modes verified successfully."
