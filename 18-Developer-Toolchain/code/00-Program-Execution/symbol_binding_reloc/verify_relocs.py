#!/usr/bin/env python3
"""Deterministic verification of static ELF relocations against linked binary words.

Parses 64-bit ELF executables, extracts symbol values and section headers,
and verifies that the linker patched instructions according to psABI formulas:
- AArch64: R_AARCH64_ABS64, R_AARCH64_ADR_PREL_PG_HI21, R_AARCH64_ADD_ABS_LO12_NC, R_AARCH64_CALL26
- x86-64:  R_X86_64_64, R_X86_64_PC32, R_X86_64_PLT32

Usage: verify_relocs.py <linked-exe> <asm.o> <target.o>
The two relocatable objects are read first, so each check proves the relocation TYPE the assembler
emitted as well as the arithmetic the linker applied.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def parse_elf(path: Path):
    data = path.read_bytes()
    assert data[:4] == b"\x7fELF", f"{path} is not an ELF binary"
    assert data[4] == 2, "Only 64-bit ELF is supported"
    assert data[5] == 1, "Only little-endian ELF is supported"

    ehdr = struct.unpack_from("<16sHHIQQQIHHHHHH", data, 0)
    e_machine = ehdr[2]
    e_shoff, e_shentsize, e_shnum, e_shstrndx = ehdr[6], ehdr[11], ehdr[12], ehdr[13]

    sections = []
    shdr_fmt = "<IIQQQQIIQQ"
    for i in range(e_shnum):
        sh = struct.unpack_from(shdr_fmt, data, e_shoff + i * e_shentsize)
        sections.append({
            "name_idx": sh[0], "type": sh[1], "flags": sh[2],
            "addr": sh[3], "offset": sh[4], "size": sh[5],
            "link": sh[6], "info": sh[7], "align": sh[8], "entsize": sh[9],
        })

    shstrtab = data[sections[e_shstrndx]["offset"] : sections[e_shstrndx]["offset"] + sections[e_shstrndx]["size"]]
    for s in sections:
        end = shstrtab.find(b"\x00", s["name_idx"])
        s["name"] = shstrtab[s["name_idx"]:end].decode("ascii", errors="replace")

    symbols = {}
    symtab_sec = next((s for s in sections if s["type"] == 2), None)
    if symtab_sec:
        strtab_sec = sections[symtab_sec["link"]]
        strtab = data[strtab_sec["offset"] : strtab_sec["offset"] + strtab_sec["size"]]

        def get_str(idx):
            end = strtab.find(b"\x00", idx)
            return strtab[idx:end].decode("ascii", errors="replace")

        for i in range(0, symtab_sec["size"], 24):
            sym = struct.unpack_from("<IBBHQQ", data, symtab_sec["offset"] + i)
            name = get_str(sym[0])
            if name:
                symbols[name] = {
                    "bind": sym[1] >> 4, "type": sym[1] & 0xf,
                    "vis": sym[2] & 3, "shndx": sym[3], "value": sym[4], "size": sym[5],
                }

    return data, sections, symbols, e_machine


def parse_relocs(path: Path):
    """Return {(section name, r_offset): (symbol name, type, addend)} for every SHT_RELA entry."""
    data, sections, _, _ = parse_elf(path)
    out = {}
    for rel in (s for s in sections if s["type"] == 4):
        target = sections[rel["info"]]["name"]
        symtab = sections[rel["link"]]
        strtab = sections[symtab["link"]]
        for i in range(0, rel["size"], 24):
            r_offset, r_info, r_addend = struct.unpack_from("<QQq", data, rel["offset"] + i)
            st_name = struct.unpack_from("<I", data, symtab["offset"] + (r_info >> 32) * 24)[0]
            start = strtab["offset"] + st_name
            name = data[start : data.index(b"\x00", start)].decode("ascii")
            out[(target, r_offset)] = (name, r_info & 0xFFFFFFFF, r_addend)
    return out


def require_reloc(relocs, section, offset, symbol, reloc_type, label):
    got = relocs.get((section, offset))
    assert got is not None, f"{label}: no relocation at {section}+{offset:#x}"
    assert got[0] == symbol and got[1] == reloc_type, (
        f"{label}: expected {symbol} type {reloc_type} at {section}+{offset:#x}, got {got[0]} type {got[1]}"
    )


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <elf_binary> <asm.o> <target.o>")
        sys.exit(1)

    binary_path = Path(sys.argv[1])
    asm_path, target_path = Path(sys.argv[2]), Path(sys.argv[3])
    asm_relocs, target_relocs = parse_relocs(asm_path), parse_relocs(target_path)
    asm_syms, target_syms = parse_elf(asm_path)[2], parse_elf(target_path)[2]
    caller_off, call_off = asm_syms["caller"]["value"], asm_syms["do_call"]["value"]
    ptr_off = target_syms["ptr_target"]["value"]
    data, sections, symbols, e_machine = parse_elf(binary_path)
    e_type = struct.unpack_from("<H", data, 16)[0]
    assert e_type == 2, f"expected a non-PIE ET_EXEC link, got e_type {e_type}"

    text_sec = next(s for s in sections if s["name"] == ".text")
    data_sec = next(s for s in sections if s["name"] == ".data")

    def read_u32(addr: int) -> int:
        offset = text_sec["offset"] + (addr - text_sec["addr"])
        return struct.unpack_from("<I", data, offset)[0]

    def read_u64(addr: int) -> int:
        offset = data_sec["offset"] + (addr - data_sec["addr"])
        return struct.unpack_from("<Q", data, offset)[0]

    s_var = symbols["target_var"]["value"]
    s_ptr = symbols["ptr_target"]["value"]
    s_caller = symbols["caller"]["value"]
    s_func = symbols["target_func"]["value"]
    s_call = symbols["do_call"]["value"]

    arch_str = "aarch64" if e_machine == 183 else ("x86_64" if e_machine == 62 else f"unknown_{e_machine}")
    print(f"Architecture: {arch_str}")

    if e_machine == 183:  # AArch64
        # 1. R_AARCH64_ABS64 at ptr_target: S + A
        require_reloc(target_relocs, ".data", ptr_off, "target_var", 257, "R_AARCH64_ABS64")
        actual_ptr = read_u64(s_ptr)
        assert actual_ptr == s_var, f"ABS64 failed: 0x{actual_ptr:x} != 0x{s_var:x}"
        print("Relocation R_AARCH64_ABS64: S + A -> MATCH")

        # 2. R_AARCH64_ADR_PREL_PG_HI21 at caller: Page(S + A) - Page(P)
        require_reloc(asm_relocs, ".text", caller_off, "target_var", 275, "R_AARCH64_ADR_PREL_PG_HI21")
        p_adrp = s_caller
        page_diff = ((s_var & ~0xfff) - (p_adrp & ~0xfff)) >> 12
        immlo = (page_diff & 0x3) << 29
        immhi = ((page_diff >> 2) & 0x7ffff) << 5
        expected_adrp = 0x90000000 | immlo | immhi
        actual_adrp = read_u32(p_adrp)
        assert actual_adrp == expected_adrp, f"ADRP failed: 0x{actual_adrp:08x} != 0x{expected_adrp:08x}"
        print("Relocation R_AARCH64_ADR_PREL_PG_HI21: Page(S + A) - Page(P) -> MATCH")

        # 3. R_AARCH64_ADD_ABS_LO12_NC at caller + 4: (S + A) & 0xFFF
        require_reloc(asm_relocs, ".text", caller_off + 4, "target_var", 277, "R_AARCH64_ADD_ABS_LO12_NC")
        p_add = s_caller + 4
        expected_add = 0x91000000 | ((s_var & 0xfff) << 10)
        actual_add = read_u32(p_add)
        assert actual_add == expected_add, f"ADD failed: 0x{actual_add:08x} != 0x{expected_add:08x}"
        print("Relocation R_AARCH64_ADD_ABS_LO12_NC: (S + A) & 0xFFF -> MATCH")

        # 4. R_AARCH64_CALL26 at do_call + 8: S + A - P
        require_reloc(asm_relocs, ".text", call_off + 8, "target_func", 283, "R_AARCH64_CALL26")
        p_bl = s_call + 8
        imm26 = ((s_func - p_bl) >> 2) & 0x03ffffff
        expected_bl = 0x94000000 | imm26
        actual_bl = read_u32(p_bl)
        assert actual_bl == expected_bl, f"BL failed: 0x{actual_bl:08x} != 0x{expected_bl:08x}"
        print("Relocation R_AARCH64_CALL26: S + A - P -> MATCH")

    elif e_machine == 62:  # x86-64
        # 1. R_X86_64_64 at ptr_target: S + A
        require_reloc(target_relocs, ".data", ptr_off, "target_var", 1, "R_X86_64_64")
        actual_ptr = read_u64(s_ptr)
        assert actual_ptr == s_var, f"R_X86_64_64 failed: 0x{actual_ptr:x} != 0x{s_var:x}"
        print("Relocation R_X86_64_64: S + A -> MATCH")

        # 2. R_X86_64_PC32 at caller: movl target_var(%rip), %eax (opcode 8b 05 <disp32>)
        require_reloc(asm_relocs, ".text", caller_off + 2, "target_var", 2, "R_X86_64_PC32")
        p_mov = s_caller + 2
        offset = text_sec["offset"] + (p_mov - text_sec["addr"])
        disp32 = struct.unpack_from("<i", data, offset)[0]
        next_ip = s_caller + 6
        assert s_var - next_ip == disp32, f"PC32 failed: {s_var - next_ip} != {disp32}"
        print("Relocation R_X86_64_PC32: S + A - P -> MATCH")

        # 3. R_X86_64_PLT32 at do_call: call target_func (opcode e8 <disp32>)
        require_reloc(asm_relocs, ".text", call_off + 1, "target_func", 4, "R_X86_64_PLT32")
        p_call = s_call + 1
        offset_c = text_sec["offset"] + (p_call - text_sec["addr"])
        disp_call = struct.unpack_from("<i", data, offset_c)[0]
        next_ip_c = s_call + 5
        assert s_func - next_ip_c == disp_call, f"Call failed: {s_func - next_ip_c} != {disp_call}"
        print("Relocation R_X86_64_PLT32: S + A - P -> MATCH")


if __name__ == "__main__":
    main()
