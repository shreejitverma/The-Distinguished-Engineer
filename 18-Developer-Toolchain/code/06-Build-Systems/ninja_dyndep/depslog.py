"""Decode Ninja's binary .ninja_deps file (format version 4) with the standard library.

Usage: depslog.py PATH

Layout, from src/deps_log.h and src/deps_log.cc in Ninja 1.13.2:
  - the 12-byte signature "# ninjadeps\\n", then a 4-byte little-endian version (4);
  - then records, each a 4-byte size word whose high bit marks a deps record;
  - a path record is the path, NUL padding to a 4-byte boundary, and the one's
    complement of the record's own id (its position among path records);
  - a deps record is int32 [output id, mtime low 32 bits, mtime high 32 bits,
    input id...].
A later deps record for the same output replaces an earlier one.
Every check raises, so a clean run means the file matched the documented format.
Mtimes vary between runs, so the lab prints only whether each stored mtime is nonzero.
"""

from __future__ import annotations

import struct
import sys

SIGNATURE = b"# ninjadeps\n"
VERSION = 4
MAX_RECORD = (1 << 19) - 1


def main() -> int:
    data = open(sys.argv[1], "rb").read()
    if data[: len(SIGNATURE)] != SIGNATURE:
        raise SystemExit("bad signature")
    (version,) = struct.unpack_from("<i", data, len(SIGNATURE))
    if version != VERSION:
        raise SystemExit(f"unexpected version {version}")
    pos = len(SIGNATURE) + 4
    paths: list[str] = []
    deps: dict[int, tuple[int, list[int]]] = {}
    path_records = deps_records = 0
    while pos < len(data):
        (word,) = struct.unpack_from("<I", data, pos)
        pos += 4
        is_deps = bool(word >> 31)
        size = word & 0x7FFFFFFF
        if size > MAX_RECORD or pos + size > len(data):
            raise SystemExit("truncated or oversized record")
        rec = data[pos : pos + size]
        pos += size
        if is_deps:
            if size % 4:
                raise SystemExit("deps record not a multiple of 4 bytes")
            ints = struct.unpack(f"<{size // 4}i", rec)
            out_id, lo, hi, inputs = ints[0], ints[1], ints[2], list(ints[3:])
            mtime = ((hi & 0xFFFFFFFF) << 32) | (lo & 0xFFFFFFFF)
            if any(i >= len(paths) for i in inputs + [out_id]):
                raise SystemExit("deps record names an unknown path id")
            deps[out_id] = (mtime, inputs)
            deps_records += 1
        else:
            if size < 8 or size % 4:
                raise SystemExit("path record not padded to 4 bytes")
            (check,) = struct.unpack_from("<I", rec, size - 4)
            expected_id = (~check) & 0xFFFFFFFF
            if expected_id != len(paths):
                raise SystemExit("path record id check failed")
            path = rec[: size - 4].rstrip(b"\0").decode()
            paths.append(path)
            path_records += 1

    print(f"  signature {SIGNATURE[:-1].decode()!r}, version {version}")
    print(f"  {path_records} path records, {deps_records} deps records, "
          f"{len(deps)} live outputs")
    for out_id in sorted(deps):
        mtime, inputs = deps[out_id]
        names = " ".join(paths[i] for i in inputs)
        print(f"  {paths[out_id]}: mtime stored={'yes' if mtime else 'no'}; deps: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
