#!/usr/bin/env python3
"""Decode USDT probes (SystemTap SDT note format v3) from an ELF64 file, standard library only.

This is the user-space half of what bpftrace, libbpf, perf, and gdb do before they can attach:
find .note.stapsdt, decode each note, and turn the probe address and the semaphore address
into file offsets, because the kernel registers a uprobe by (inode, file offset), never by
virtual address.

Usage:
    stapsdt.py BINARY            human-readable report
    stapsdt.py --sites BINARY    one "provider:name pc_vaddr sema_vaddr" line per probe
"""

from __future__ import annotations

import re
import struct
import subprocess
import sys
from dataclasses import dataclass

EM_X86_64 = 62
EM_AARCH64 = 183
SHT_NOTE = 7
SHF_ALLOC = 0x2
PT_LOAD = 1
PF_X, PF_W = 0x1, 0x2
NT_STAPSDT = 3
AARCH64_NOP = 0xD503201F


class ElfError(Exception):
    pass


@dataclass(frozen=True)
class Section:
    name: str
    sh_type: int
    flags: int
    addr: int
    offset: int
    size: int


@dataclass(frozen=True)
class Segment:
    offset: int
    vaddr: int
    filesz: int
    flags: int


@dataclass(frozen=True)
class Probe:
    provider: str
    name: str
    pc: int
    sema: int
    args: str


class Elf:
    def __init__(self, path: str) -> None:
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        if d[:4] != b"\x7fELF":
            raise ElfError("not an ELF file")
        if d[4] != 2 or d[5] != 1:
            raise ElfError("only little-endian ELF64 is supported")
        (self.e_type, self.machine, _, _, phoff, shoff, _, _, phentsize, phnum,
         shentsize, shnum, shstrndx) = struct.unpack_from("<HHIQQQIHHHHHH", d, 16)
        raw = [struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize) for i in range(shnum)]
        strtab = raw[shstrndx]
        names = d[strtab[4]:strtab[4] + strtab[5]]

        def name_at(off: int) -> str:
            return names[off:names.index(b"\0", off)].decode()

        self.sections = [Section(name_at(s[0]), s[1], s[2], s[3], s[4], s[5]) for s in raw]
        self.segments = []
        for i in range(phnum):
            p_type, p_flags, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from(
                "<IIQQQQQQ", d, phoff + i * phentsize)
            if p_type == PT_LOAD:
                self.segments.append(Segment(p_offset, p_vaddr, p_filesz, p_flags))

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)

    def section_of(self, vaddr: int) -> Section | None:
        return next((s for s in self.sections
                     if s.flags & SHF_ALLOC and s.addr <= vaddr < s.addr + s.size), None)

    def segment_of(self, vaddr: int) -> Segment:
        for seg in self.segments:
            if seg.vaddr <= vaddr < seg.vaddr + seg.filesz:
                return seg
        raise ElfError(f"no PT_LOAD segment holds address {vaddr:#x}")

    def file_offset(self, vaddr: int) -> int:
        seg = self.segment_of(vaddr)
        return vaddr - seg.vaddr + seg.offset


def read_probes(elf: Elf) -> list[Probe]:
    sec = elf.section(".note.stapsdt")
    if sec is None:
        raise ElfError("no .note.stapsdt section: the binary has no USDT probes")
    if sec.sh_type != SHT_NOTE:
        raise ElfError(".note.stapsdt is not SHT_NOTE")
    base_sec = elf.section(".stapsdt.base")
    blob = elf.data[sec.offset:sec.offset + sec.size]
    probes, pos = [], 0
    while pos + 12 <= len(blob):
        namesz, descsz, ntype = struct.unpack_from("<III", blob, pos)
        pos += 12
        owner = blob[pos:pos + namesz]
        pos += (namesz + 3) & ~3
        desc = blob[pos:pos + descsz]
        pos += (descsz + 3) & ~3
        if owner != b"stapsdt\0" or ntype != NT_STAPSDT:
            continue
        pc, base, sema = struct.unpack_from("<QQQ", desc, 0)
        provider, name, args = (s.decode() for s in desc[24:].split(b"\0")[:3])
        # Prelink moves sections but cannot rewrite a non-allocated note, so the note carries
        # the link-time address of .stapsdt.base and the reader applies the difference.
        if base_sec is not None and base:
            shift = base_sec.addr - base
            pc += shift
            if sema:
                sema += shift
        probes.append(Probe(provider, name, pc, sema, args))
    return probes


def split_args(spec: str) -> list[str]:
    """Split on spaces that are outside brackets and parentheses: an operand may contain spaces."""
    out, depth, cur = [], 0, ""
    for ch in spec:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == " " and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def describe_arg(arg: str) -> str:
    m = re.fullmatch(r"(-?\d+)@(.+)", arg)
    if not m:
        return f"{arg} -> no size prefix, natural operand size"
    size, op = int(m.group(1)), m.group(2)
    kind = f"{'signed' if size < 0 else 'unsigned'} {abs(size)}-byte value"
    if re.fullmatch(r"\$?-?\d+", op):
        where = f"the constant {op.lstrip('$')}"
    elif mem := re.fullmatch(r"\[(\w+)(?:, #?(-?\d+))?\]", op):  # aarch64: [x4, 16]
        where = f"memory at register {mem.group(1)} + {mem.group(2) or 0}"
    elif mem := re.fullmatch(r"(-?\d+)?\(%(\w+)\)", op):  # x86-64: -8(%rbp)
        where = f"memory at register {mem.group(2)} + {mem.group(1) or 0}"
    elif reg := re.fullmatch(r"%?(\w+)", op):
        where = f"register {reg.group(1)}"
    else:
        where = f"the operand {op}"
    return f"{arg} -> {kind} in {where}"


def site_instruction(elf: Elf, pc: int) -> str:
    off = elf.file_offset(pc)
    if elf.machine == EM_AARCH64:
        return "nop" if struct.unpack_from("<I", elf.data, off)[0] == AARCH64_NOP else "NOT a nop"
    if elf.machine == EM_X86_64:
        return "nop" if elf.data[off] == 0x90 else "NOT a nop"
    raise ElfError(f"unsupported machine {elf.machine}")


def readelf_view(path: str) -> list[tuple[str, str, int, int, str]]:
    """The same notes as binutils decodes them: an independent parser to check ours against."""
    out = subprocess.run(["readelf", "-n", path], capture_output=True, text=True, check=True).stdout
    pattern = re.compile(
        r"Provider: (\S+)\s+Name: (\S+)\s+Location: (0x[0-9a-f]+), Base: 0x[0-9a-f]+, "
        r"Semaphore: (0x[0-9a-f]+)\s+Arguments: ([^\n]*)")
    return [(p, n, int(loc, 16), int(sem, 16), args.strip())
            for p, n, loc, sem, args in pattern.findall(out)]


def report(path: str) -> None:
    elf = Elf(path)
    probes = read_probes(elf)
    note = elf.section(".note.stapsdt")
    alloc = "allocated" if note.flags & SHF_ALLOC else "not allocated, so never mapped at run time"
    print(f".note.stapsdt: SHT_NOTE, {alloc}; {len(probes)} probes")
    for p in probes:
        seg = elf.segment_of(p.pc)
        sec = elf.section_of(p.pc)
        print(f"probe {p.provider}:{p.name}")
        print(f"  site: section {sec.name}, {'executable' if seg.flags & PF_X else 'NON-executable'} "
              f"segment, file offset = address - {p.pc - elf.file_offset(p.pc):#x}")
        print(f"  site instruction on disk: {site_instruction(elf, p.pc)}")
        if p.sema:
            sseg, ssec = elf.segment_of(p.sema), elf.section_of(p.sema)
            off = elf.file_offset(p.sema)
            value = struct.unpack_from("<H", elf.data, off)[0]
            print(f"  semaphore: section {ssec.name}, {'writable' if sseg.flags & PF_W else 'READ-ONLY'} "
                  f"segment, file offset = address - {p.sema - off:#x}, value on disk {value}")
        else:
            print("  semaphore: none")
        for i, arg in enumerate(split_args(p.args)):
            print(f"  arg{i}: {describe_arg(arg)}")
    ours = sorted((p.provider, p.name, p.pc, p.sema, p.args) for p in probes)
    theirs = sorted(readelf_view(path))
    matched = sum(1 for o in ours if o in theirs)
    if matched != len(ours) or len(ours) != len(theirs):
        raise ElfError(f"readelf -n disagrees: {theirs} vs {ours}")
    print(f"readelf -n decodes the same provider, name, location, semaphore, and arguments: "
          f"{matched}/{len(theirs)} probes")


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "--sites":
            for p in read_probes(Elf(argv[2])):
                print(f"{p.provider}:{p.name} {p.pc:#x} {p.sema:#x}")
        elif len(argv) == 2:
            report(argv[1])
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (ElfError, OSError, subprocess.CalledProcessError, struct.error) as e:
        print(f"stapsdt.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
