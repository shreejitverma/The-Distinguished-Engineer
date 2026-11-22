"""Minimal ELF64 little-endian dynamic symbol reader for the drill.

Usage:
  python3 -I elf_ifunc.py summary LIB
  python3 -I elf_ifunc.py compare LIB_A LIB_B

Constants from glibc elf/elf.h: STT_FUNC 2, STT_GNU_IFUNC 10, SHT_SYMTAB 2, SHT_DYNSYM 11,
EM_X86_64 62, EM_AARCH64 183, SHN_UNDEF 0.
"""

import hashlib
import struct
import sys

STT = {0: "NOTYPE", 1: "OBJECT", 2: "FUNC", 3: "SECTION", 4: "FILE", 5: "COMMON", 6: "TLS", 10: "IFUNC"}
STB = {0: "LOCAL", 1: "GLOBAL", 2: "WEAK", 10: "UNIQUE"}
SHT_SYMTAB, SHT_DYNSYM = 2, 11
SHF_EXECINSTR = 4


class Elf:
    def __init__(self, path):
        d = open(path, "rb").read()
        if d[:4] != b"\x7fELF" or d[4] != 2 or d[5] != 1:
            raise SystemExit(f"{path}: not a little-endian ELF64 file")
        self.data = d
        (self.machine,) = struct.unpack_from("<H", d, 18)
        (shoff,) = struct.unpack_from("<Q", d, 0x28)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x3A)
        self.sections = []
        for i in range(shnum):
            f = struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize)
            self.sections.append(
                dict(name_off=f[0], type=f[1], flags=f[2], addr=f[3], offset=f[4], size=f[5], link=f[6], entsize=f[9])
            )
        stroff = self.sections[shstrndx]["offset"]
        for s in self.sections:
            s["name"] = self._cstr(stroff + s["name_off"])

    def _cstr(self, off):
        end = self.data.index(b"\0", off)
        return self.data[off:end].decode("ascii", "replace")

    def symbols(self, sh_type):
        out = []
        for s in self.sections:
            if s["type"] != sh_type:
                continue
            strtab = self.sections[s["link"]]["offset"]
            for i in range(s["size"] // 24):
                name, info, other, shndx, value, size = struct.unpack_from("<IBBHQQ", self.data, s["offset"] + i * 24)
                if i == 0:
                    continue
                out.append(
                    dict(name=self._cstr(strtab + name), type=STT.get(info & 0xF, str(info & 0xF)),
                         bind=STB.get(info >> 4, str(info >> 4)), shndx=shndx, value=value, size=size)
                )
        return out

    def section(self, name):
        return next((s for s in self.sections if s["name"] == name), None)

    def text_sha(self):
        s = self.section(".text")
        return hashlib.sha256(self.data[s["offset"] : s["offset"] + s["size"]]).hexdigest()

    def resolver_size(self, ifunc_sym):
        """Size of the resolver as recorded in .symtab (a local FUNC at the same address), else None."""
        for s in self.symbols(SHT_SYMTAB):
            if s["type"] == "FUNC" and s["value"] == ifunc_sym["value"] and s["size"] > 0:
                return s["size"]
        return ifunc_sym["size"] or None


def dyn_view(e):
    syms = e.symbols(SHT_DYNSYM)
    defined = {(s["name"], s["type"], s["bind"]) for s in syms if s["shndx"] != 0}
    undefined = {s["name"] for s in syms if s["shndx"] == 0}
    ifuncs = {s["name"]: s for s in syms if s["type"] == "IFUNC" and s["shndx"] != 0}
    return defined, undefined, ifuncs


def summary(path):
    e = Elf(path)
    defined, undefined, ifuncs = dyn_view(e)
    print(f"  dynsym defined: {len(defined)}, undefined: {len(undefined)}, IFUNC: {len(ifuncs)}")
    for name, s in sorted(ifuncs.items()):
        print(f"  IFUNC {name}: resolver size {e.resolver_size(s)}")


def compare(a, b):
    ea, eb = Elf(a), Elf(b)
    da, ua, ia = dyn_view(ea)
    db, ub, ib = dyn_view(eb)
    print(f"dynsym defined (name, type, bind) sets equal: {'yes' if da == db else 'no'}")
    print(f"dynsym undefined (imported) sets equal: {'yes' if ua == ub else 'no'}")
    print(f"IFUNC symbol names equal: {'yes' if set(ia) == set(ib) else 'no'} {sorted(ia)} vs {sorted(ib)}")
    for name in sorted(set(ia) & set(ib)):
        ra, rb = ea.resolver_size(ia[name]), eb.resolver_size(ib[name])
        print(f"IFUNC {name} resolver size: {ra} vs {rb}: {'equal' if ra == rb else 'DIFFERENT'}")
    print(f".text sha256 equal: {'yes' if ea.text_sha() == eb.text_sha() else 'no'}")
    print(f"whole file bytes equal: {'yes' if ea.data == eb.data else 'no'}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "summary":
        summary(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(2)
