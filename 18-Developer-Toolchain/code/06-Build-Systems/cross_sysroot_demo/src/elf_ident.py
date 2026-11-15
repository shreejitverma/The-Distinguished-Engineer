"""Print the ELF identity fields a loader and a linker check first: class, byte order, e_machine.

Reads the header directly instead of parsing readelf text, so big-endian objects decode correctly.
"""

import struct
import sys


def main(path: str) -> None:
    with open(path, "rb") as f:
        head = f.read(20)
    if head[:4] != b"\x7fELF":
        sys.exit(f"{path}: not an ELF file")
    elf_class = {1: "ELF32", 2: "ELF64"}[head[4]]
    order = {1: "<", 2: ">"}[head[5]]
    endian = {1: "LE", 2: "BE"}[head[5]]
    (e_machine,) = struct.unpack(order + "H", head[18:20])
    print(f"{elf_class} {endian} e_machine={e_machine}")


if __name__ == "__main__":
    main(sys.argv[1])
