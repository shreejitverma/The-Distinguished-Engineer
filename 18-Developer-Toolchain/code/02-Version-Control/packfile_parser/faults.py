#!/usr/bin/env python3
"""Silent fault-injection checks for packfile.py.

The lab repository is small and well formed, so most of the parser's defences never fire on it.
This script proves each defence by feeding the parser a pack that breaks exactly one rule and
asserting the specific error.
It also parses a wide REF_DELTA pack that exercises the binary search and a hand-built idx with a
large-offset table.
It prints nothing on success and raises on the first failed expectation.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

from packfile import MAX_CHAIN, apply_delta, parse_packfile

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Toolchain Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Toolchain Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "LC_ALL": "C",
}


def git(repo: str, *args: str, data: bytes | None = None) -> None:
    subprocess.run(["git", *args], cwd=repo, input=data, env=GIT_ENV, check=True, capture_output=True)


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        c = n & 0x7F
        n >>= 7
        out.append(c | (0x80 if n else 0))
        if not n:
            return bytes(out)


def delta_header(base_size: int, target_size: int) -> bytes:
    return varint(base_size) + varint(target_size)


def check_apply_delta() -> None:
    base = bytes(range(256)) * 256  # 65536 bytes
    # A copy with no size bytes means 0x10000, the maximum single copy.
    assert apply_delta(base, delta_header(len(base), len(base)) + b"\x80") == base
    # Insert 3 bytes, then copy 4 bytes from offset 3 (flags 0x91: one offset byte, one size byte).
    got = apply_delta(base, delta_header(len(base), 7) + b"\x03abc" + b"\x91\x03\x04")
    assert got == b"abc" + base[3:7], got
    bad_deltas = (
        (delta_header(len(base), 0) + b"\x00", "reserved opcode"),
        (delta_header(len(base), 2) + b"\x05ab", "insert past the end of the delta"),
        (delta_header(10, 1) + b"\x01a", "base size mismatch"),
        (delta_header(len(base), 1) + b"\x93\xff\xff\x10", "copy past the end of the base"),
        (delta_header(len(base), 2) + b"\x01a", "target size mismatch"),
    )
    for bad, why in bad_deltas:
        try:
            apply_delta(base, bad)
        except ValueError:
            continue
        raise AssertionError(f"apply_delta accepted a bad delta: {why}")


def parse_quietly(repo: str):
    with contextlib.redirect_stdout(io.StringIO()):
        return parse_packfile(repo)


def expect_error(repo: str, needle: str) -> None:
    try:
        parse_quietly(repo)
    except ValueError as exc:
        if needle not in str(exc):
            raise AssertionError(f"expected {needle!r}, got {exc!r}") from exc
        return
    raise AssertionError(f"parser accepted a pack that should fail with {needle!r}")


def pack_paths(repo: str) -> tuple[str, str]:
    d = os.path.join(repo, ".git", "objects", "pack")
    idx = next(f for f in sorted(os.listdir(d)) if f.endswith(".idx"))
    return os.path.join(d, idx), os.path.join(d, idx[:-4] + ".pack")


def seal(pack: bytearray, idx: bytearray, *, pack_trailer: bool = True, link: bool = True, idx_trailer: bool = True) -> None:
    """Recompute the three trailers so that only the fault under test is left broken."""
    if pack_trailer:
        pack[-20:] = hashlib.sha1(pack[:-20]).digest()
    if link:
        idx[-40:-20] = pack[-20:]
    if idx_trailer:
        idx[-20:] = hashlib.sha1(idx[:-20]).digest()


def corrupt_lab_repo(repo: str) -> None:
    total = parse_quietly(repo)[0].__len__()
    cases = (
        ("pack=False idx=True link=True", lambda p, i: (p.__setitem__(-1, p[-1] ^ 1), seal(p, i, pack_trailer=False))),
        ("pack=True idx=False link=True", lambda p, i: (i.__setitem__(-1, i[-1] ^ 1), seal(p, i, pack_trailer=False, link=False, idx_trailer=False))),
        ("pack=True idx=True link=False", lambda p, i: (i.__setitem__(-30, i[-30] ^ 1), seal(p, i, pack_trailer=False, link=False))),
        ("fanout table is not monotonic", lambda p, i: (i.__setitem__(slice(8, 12), struct.pack(">I", total + 1)), seal(p, i, pack_trailer=False, link=False))),
        ("Invalid pack header", lambda p, i: (p.__setitem__(slice(8, 12), struct.pack(">I", total + 1)), seal(p, i))),
        ("CRC32 mismatch", lambda p, i: (i.__setitem__(8 + 1024 + total * 20, i[8 + 1024 + total * 20] ^ 1), seal(p, i, pack_trailer=False, link=False))),
    )
    for needle, mutate in cases:
        work = tempfile.mkdtemp(prefix="faults-")
        try:
            shutil.copytree(os.path.join(repo, ".git"), os.path.join(work, ".git"))
            idx_path, pack_path = pack_paths(work)
            pack, idx = bytearray(open(pack_path, "rb").read()), bytearray(open(idx_path, "rb").read())
            mutate(pack, idx)
            for path, data in ((pack_path, pack), (idx_path, idx)):
                os.unlink(path)  # git writes packs read-only; replace the file instead of reopening it
                open(path, "wb").write(data)
            expect_error(work, needle)
        finally:
            shutil.rmtree(work)


def entry(kind: int, payload: bytes, base: bytes | None = None, declared: int | None = None) -> bytes:
    size = len(payload) if declared is None else declared
    head = bytearray([(kind << 4) | (size & 15) | (0x80 if size >> 4 else 0)])
    size >>= 4
    while size:
        head.append((size & 0x7F) | (0x80 if size >> 7 else 0))
        size >>= 7
    return bytes(head) + (base or b"") + zlib.compress(payload)


def build_pack(objects: list[tuple[bytes, bytes]], *, large_offsets: bool = False) -> tuple[bytes, bytes]:
    """Return (pack, idx) for objects given as (name, encoded entry), in pack order."""
    pack = bytearray(b"PACK" + struct.pack(">II", 2, len(objects)))
    offsets = {}
    for name, enc in objects:
        offsets[name] = len(pack)
        pack += enc
    pack += hashlib.sha1(pack).digest()
    names = sorted(offsets)
    encoded = dict(objects)
    fan = [0] * 256
    for n in names:
        fan[n[0]] += 1
    fanout = list(__import__("itertools").accumulate(fan))
    idx = bytearray(b"\xfftOc" + struct.pack(">I", 2) + struct.pack(">256I", *fanout))
    idx += b"".join(names)
    for n in names:
        idx += struct.pack(">I", zlib.crc32(pack[offsets[n] : offsets[n] + len(encoded[n])]))
    big = bytearray()
    for k, n in enumerate(names):
        # Git rejects an idx whose large-offset table has more than N-1 entries, so entry 0 stays direct.
        if large_offsets and k > 0:
            idx += struct.pack(">I", 0x80000000 | (k - 1))
            big += struct.pack(">Q", offsets[n])
        else:
            idx += struct.pack(">I", offsets[n])
    idx += big + pack[-20:]
    idx += hashlib.sha1(idx).digest()
    return bytes(pack), bytes(idx)


def install(repo: str, pack: bytes, idx: bytes) -> None:
    d = os.path.join(repo, ".git", "objects", "pack")
    os.makedirs(d, exist_ok=True)
    stem = os.path.join(d, "pack-" + pack[-20:].hex())
    open(stem + ".pack", "wb").write(pack)
    open(stem + ".idx", "wb").write(idx)


def fresh_repo(parent: str, name: str) -> str:
    repo = os.path.join(parent, name)
    os.mkdir(repo)
    git(repo, "init", "-q", "-b", "main")
    return repo


def blob_oid(data: bytes) -> bytes:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).digest()


def hostile_packs(parent: str) -> None:
    ref = lambda i: i.to_bytes(20, "big")  # noqa: E731 - arbitrary names; the walk fails before any SHA-1 check
    empty_delta = delta_header(0, 0)

    repo = fresh_repo(parent, "cycle")
    a, b = ref(1), ref(2)
    install(repo, *build_pack([(a, entry(7, empty_delta, base=b)), (b, entry(7, empty_delta, base=a))]))
    expect_error(repo, "delta cycle detected")

    repo = fresh_repo(parent, "deep")
    n = MAX_CHAIN + 3
    objs = [(ref(i + 1), entry(7, empty_delta, base=ref(i + 2))) for i in range(n)]
    objs.append((ref(n + 1), entry(3, b"x")))
    install(repo, *build_pack(objs))
    expect_error(repo, "delta chain longer than")

    # Same depth, but a mid-chain object sorts first, so its half of the chain is cached before
    # the deepest object is walked; the cap must count the cached depth, not just the new deltas.
    repo = fresh_repo(parent, "deep-cached")
    datas = [b"chain node %d\n" % i for i in range(n + 1)]
    first = min(datas, key=blob_oid)
    datas.remove(first)
    datas.insert(n // 2, first)
    objs = [
        (blob_oid(d), entry(7, delta_header(len(datas[i + 1]), len(d)) + bytes([len(d)]) + d, base=blob_oid(datas[i + 1])))
        for i, d in enumerate(datas[:-1])
    ]
    objs.append((blob_oid(datas[-1]), entry(3, datas[-1])))
    install(repo, *build_pack(objs))
    expect_error(repo, "delta chain longer than")

    repo = fresh_repo(parent, "lying-size")
    install(repo, *build_pack([(ref(1), entry(3, b"abc", declared=5))]))
    expect_error(repo, "inflated size mismatch")


def large_offset_pack(parent: str) -> None:
    repo = fresh_repo(parent, "large")
    datas = [b"large offset blob %d\n" % i for i in range(5)]
    install(repo, *build_pack([(blob_oid(d), entry(3, d)) for d in datas], large_offsets=True))
    info, _ = parse_quietly(repo)
    assert {o: v[1] for o, v in info.items()} == {blob_oid(d).hex(): len(d) for d in datas}


def wide_ref_delta_pack(parent: str) -> None:
    """1200 similar blobs repacked with REF_DELTA, so first-byte buckets hold several names."""
    repo = fresh_repo(parent, "wide")
    common = "".join(f"line {i:03d} shared by every file in the wide pack\n" for i in range(80))
    stream = bytearray()
    count = 1200
    for i in range(count):
        body = (common + f"unique line {i}\n").encode()
        stream += b"blob\nmark :%d\ndata %d\n" % (i + 1, len(body)) + body + b"\n"
    msg = b"wide"
    stream += b"commit refs/heads/main\ncommitter A <a@example.com> 0 +0000\ndata %d\n%s\n" % (len(msg), msg)
    for i in range(count):
        stream += b"M 100644 :%d f%04d\n" % (i + 1, i)
    git(repo, "fast-import", "--quiet", data=bytes(stream))
    git(repo, "-c", "repack.useDeltaBaseOffset=false", "-c", "pack.threads=1", "repack", "-a", "-d", "-f", "-q", "--window=50", "--depth=50")
    info, raw_types = parse_quietly(repo)
    assert len(info) == count + 2, len(info)
    assert raw_types[7] > 100, raw_types  # REF_DELTA entries, resolved through the name table
    assert raw_types[6] == 0, raw_types
    biggest_bucket = max(collections.Counter(o[:2] for o in info).values())
    assert biggest_bucket >= 8, biggest_bucket  # several names share a first byte, so the search loops


def main() -> None:
    repo = sys.argv[1]
    check_apply_delta()
    corrupt_lab_repo(repo)
    parent = tempfile.mkdtemp(prefix="faults-")
    try:
        hostile_packs(parent)
        large_offset_pack(parent)
        wide_ref_delta_pack(parent)
    finally:
        shutil.rmtree(parent)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: faults.py <repo-path>", file=sys.stderr)
        sys.exit(1)
    main()
