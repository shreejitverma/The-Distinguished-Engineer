#!/usr/bin/env python3
"""Decode .eh_frame_hdr of a little-endian ELF64 binary and check it against .eh_frame and .symtab.

Pure struct parsing, no readelf. Exits 1 if any check fails, so the lab cannot pass on a missing or
wrong index. Only the encodings GCC and Clang emit for the header are accepted:
eh_frame_ptr 0x1b (pcrel|sdata4), fde_count 0x03 (udata4), table 0x3b (datarel|sdata4).
"""
import struct
import sys

PT_GNU_EH_FRAME = 0x6474E550
STT_FUNC = 2
LAB_FUNCTIONS = ("leaf", "middle", "outer", "main")


def uleb(data, pos):
    result = shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, pos


def read_elf(path):
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:4] != b"\x7fELF" or blob[4] != 2 or blob[5] != 1:
        sys.exit("not a little-endian ELF64 file")
    e_phoff, e_shoff = struct.unpack_from("<QQ", blob, 0x20)
    e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHHHH", blob, 0x36)
    sections = []
    for i in range(e_shnum):
        name, typ, _flags, addr, off, size, link, _info, _align, entsize = struct.unpack_from(
            "<IIQQQQIIQQ", blob, e_shoff + i * e_shentsize)
        sections.append(dict(name_off=name, type=typ, addr=addr, off=off, size=size, link=link, entsize=entsize))
    strtab = sections[e_shstrndx]
    for s in sections:
        start = strtab["off"] + s["name_off"]
        s["name"] = blob[start:blob.index(b"\0", start)].decode("ascii")
    phdrs = []
    for i in range(e_phnum):
        typ, _flags, off, vaddr, _paddr, filesz, _memsz, _align = struct.unpack_from(
            "<IIQQQQQQ", blob, e_phoff + i * e_phentsize)
        phdrs.append(dict(type=typ, off=off, vaddr=vaddr, filesz=filesz))
    return blob, sections, phdrs


def section(sections, name):
    for s in sections:
        if s["name"] == name:
            return s
    sys.exit(f"Error: section {name} not found")


def function_symbols(blob, sections):
    symtab = section(sections, ".symtab")
    strtab = sections[symtab["link"]]
    found = {}
    for i in range(symtab["size"] // symtab["entsize"]):
        st_name, st_info, _other, _shndx, st_value, _size = struct.unpack_from(
            "<IBBHQQ", blob, symtab["off"] + i * symtab["entsize"])
        if st_info & 0xF == STT_FUNC:
            start = strtab["off"] + st_name
            name = blob[start:blob.index(b"\0", start)].decode("ascii")
            if name in LAB_FUNCTIONS:
                found[name] = st_value
    return found


def fde_pc_begin(blob, eh_frame, fde_addr):
    """Return the absolute start address stored in the FDE at fde_addr, asserting the CIE encoding."""
    base = eh_frame["off"] - eh_frame["addr"]
    pos = fde_addr + base
    length, cie_ptr = struct.unpack_from("<II", blob, pos)
    if length in (0, 0xFFFFFFFF) or cie_ptr == 0:
        sys.exit("FDE expected, found a CIE or an extended-length record")
    cie = pos + 4 - cie_ptr
    cie_len, cie_id = struct.unpack_from("<II", blob, cie)
    if cie_id != 0:
        sys.exit("CIE pointer does not reach a CIE")
    p = cie + 8
    p += 1  # version
    end = blob.index(b"\0", p)
    aug = blob[p:end].decode("ascii")
    p = end + 1
    _, p = uleb(blob, p)  # code alignment
    while blob[p] & 0x80:  # data alignment (sleb)
        p += 1
    p += 1
    p += 1  # return address column (version 1)
    if not aug.startswith("z"):
        sys.exit(f"unexpected augmentation {aug!r}")
    _, p = uleb(blob, p)  # augmentation length
    r_enc = None
    for ch in aug[1:]:
        if ch == "R":
            r_enc = blob[p]
            p += 1
        elif ch == "L":
            p += 1
        elif ch == "P":
            enc = blob[p]
            p += 1 + (8 if enc & 0x07 == 0x04 else 4)
    if r_enc != 0x1B:
        sys.exit(f"FDE pointer encoding {r_enc} is not 0x1b (pcrel|sdata4)")
    field = pos + 8
    (rel,) = struct.unpack_from("<i", blob, field)
    return (field - base) + rel


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: parse_eh_frame_hdr.py <binary>")
    blob, sections, phdrs = read_elf(sys.argv[1])
    hdr = section(sections, ".eh_frame_hdr")
    eh_frame = section(sections, ".eh_frame")
    data = blob[hdr["off"]:hdr["off"] + hdr["size"]]
    version, ptr_enc, count_enc, table_enc = data[:4]
    (ptr_rel,) = struct.unpack_from("<i", data, 4)
    (fde_count,) = struct.unpack_from("<I", data, 8)

    checks = []
    print(f"  Header: version={version} eh_frame_ptr_enc=0x{ptr_enc:02x} "
          f"fde_count_enc=0x{count_enc:02x} table_enc=0x{table_enc:02x}")
    checks.append(("Header is version 1 with the GCC and Clang encodings",
                   (version, ptr_enc, count_enc, table_enc) == (1, 0x1B, 0x03, 0x3B)))
    seg = [p for p in phdrs if p["type"] == PT_GNU_EH_FRAME]
    checks.append(("PT_GNU_EH_FRAME covers .eh_frame_hdr",
                   len(seg) == 1 and seg[0]["off"] == hdr["off"] and seg[0]["vaddr"] == hdr["addr"]
                   and seg[0]["filesz"] == hdr["size"]))
    checks.append(("eh_frame_ptr resolves to the start of .eh_frame", hdr["addr"] + 4 + ptr_rel == eh_frame["addr"]))
    checks.append(("fde_count entries fit in the section", 12 + 8 * fde_count == hdr["size"]))

    entries = [struct.unpack_from("<ii", data, 12 + i * 8) for i in range(fde_count)]
    locs = [hdr["addr"] + loc for loc, _ in entries]
    checks.append(("Binary search table is strictly sorted by function start",
                   all(a < b for a, b in zip(locs, locs[1:]))))
    inside = all(eh_frame["addr"] <= hdr["addr"] + fde < eh_frame["addr"] + eh_frame["size"] for _, fde in entries)
    checks.append(("Every FDE pointer lands inside .eh_frame", inside))
    agree = inside and all(fde_pc_begin(blob, eh_frame, hdr["addr"] + fde) == loc
                           for loc, (_, fde) in zip(locs, entries))
    checks.append(("Every table entry equals the pc_begin of the FDE it points to", agree))
    syms = function_symbols(blob, sections)
    checks.append(("leaf, middle, outer, and main are each indexed at their symbol address",
                   set(syms) == set(LAB_FUNCTIONS) and all(addr in set(locs) for addr in syms.values())))

    failed = 0
    for text, ok in checks:
        print(f"  {text}: {ok}")
        failed += not ok
    if failed:
        sys.exit(f"{failed} .eh_frame_hdr check(s) failed")


if __name__ == "__main__":
    main()
