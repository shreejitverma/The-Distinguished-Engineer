#!/usr/bin/env python3
"""Inspect and decode Multi-Pack-Index (MIDX) and Commit-Graph chunk formats.

Every structure is decoded by hand and then cross-checked against what git itself
reports (git show-index, git cat-file, git log, git diff-tree), so a wrong decode or a
missing mechanism fails an assert instead of printing plausible numbers.
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys

HASHES = {1: ("sha1", 20), 2: ("sha256", 32)}
NULL_PARENT = 0x70000000
EDGE_FLAG = 0x80000000


def git(repo_dir: str, *args: str, stdin: bytes | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        input=stdin,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr!r}"
    return proc.stdout.decode("utf-8")


def read_toc(
    data: bytes, start: int, nchunks: int, hash_len: int
) -> tuple[list[tuple[bytes, int]], dict[bytes, bytes]]:
    """Decode a chunk table of contents (gitformat-chunk) and slice every chunk.

    A chunk ends where the next TOC entry begins, so no chunk order is assumed.
    """
    toc: list[tuple[bytes, int]] = []
    for i in range(nchunks + 1):
        pos = start + i * 12
        cid, off = struct.unpack(">4sQ", data[pos : pos + 12])
        toc.append((cid, off))
    assert toc[-1][0] == b"\x00\x00\x00\x00", "TOC must end with a zero terminator"
    assert toc[-1][1] == len(data) - hash_len, "terminator must point at the trailer"
    assert toc[0][1] == start + (nchunks + 1) * 12, "first chunk follows the TOC"
    ids = [cid for cid, _ in toc]
    assert len(set(ids)) == len(ids), "duplicate chunk id"
    offsets = [off for _, off in toc]
    assert offsets == sorted(offsets), "chunk offsets must not decrease"
    chunks = {
        cid: data[off : toc[i + 1][1]] for i, (cid, off) in enumerate(toc[:-1])
    }
    return toc, chunks


def print_toc(toc: list[tuple[bytes, int]]) -> None:
    for cid, off in toc:
        name = cid.decode("latin1") if any(cid) else "TERMINATOR"
        print(f"  Chunk {name:10s} : byte_offset={off:5d}")


def check_fanout(fanout: tuple[int, ...], oids: list[bytes]) -> None:
    assert list(fanout) == sorted(fanout), "fanout must be non-decreasing"
    assert fanout[255] == len(oids), "fanout[255] must equal the object count"
    assert oids == sorted(oids) and len(set(oids)) == len(oids), "OIDL must be sorted"
    for first_byte in range(256):
        expected = sum(1 for o in oids if o[0] <= first_byte)
        assert fanout[first_byte] == expected, f"fanout[{first_byte}] is wrong"


def murmur3_v1(seed: int, data: bytes) -> int:
    """32-bit murmur3 as in bloom.c (ASCII paths only, so v1 and v2 agree)."""
    assert all(b < 0x80 for b in data), "signed-char v1 quirk not modeled"
    mask = 0xFFFFFFFF
    c1, c2, n = 0xCC9E2D51, 0x1B873593, 0xE6546B64
    h = seed
    nblocks = len(data) // 4
    for i in range(nblocks):
        k = struct.unpack("<I", data[4 * i : 4 * i + 4])[0]
        k = (k * c1) & mask
        k = ((k << 15) | (k >> 17)) & mask
        k = (k * c2) & mask
        h ^= k
        h = ((h << 13) | (h >> 19)) & mask
        h = (h * 5 + n) & mask
    tail = data[4 * nblocks :]
    k1 = 0
    for j in range(len(tail) - 1, -1, -1):
        k1 ^= tail[j] << (8 * j)
    if tail:
        k1 = (k1 * c1) & mask
        k1 = ((k1 << 15) | (k1 >> 17)) & mask
        k1 = (k1 * c2) & mask
        h ^= k1
    h ^= len(data)
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & mask
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & mask
    h ^= h >> 16
    return h


def bloom_contains(filt: bytes, path: bytes, num_hashes: int) -> bool:
    """Query a changed-path Bloom filter the way bloom_filter_contains() does."""
    if not filt:
        return False
    h0 = murmur3_v1(0x293AE76F, path)
    h1 = murmur3_v1(0x7E646E2C, path)
    nbits = len(filt) * 8
    for i in range(num_hashes):
        pos = ((h0 + i * h1) & 0xFFFFFFFF) % nbits
        if not filt[pos // 8] & (1 << (pos & 7)):
            return False
    return True


def inspect_midx(repo_dir: str, preferred: str | None) -> None:
    pack_dir = os.path.join(repo_dir, ".git", "objects", "pack")
    midx_file = os.path.join(pack_dir, "multi-pack-index")
    if not os.path.exists(midx_file):
        print(f"Error: {midx_file} not found", file=sys.stderr)
        sys.exit(1)

    with open(midx_file, "rb") as f:
        midx_data = f.read()

    assert midx_data[:4] == b"MIDX", "Invalid MIDX signature"
    ver, oid_ver, num_chunks, num_base, num_packs = struct.unpack(
        ">BBBB I", midx_data[4:12]
    )
    assert ver == 1, "this lab decodes MIDX version 1 only"
    assert oid_ver in HASHES, f"unknown MIDX object id version {oid_ver}"
    hash_name, hl = HASHES[oid_ver]
    trailer_midx = midx_data[-hl:]
    computed_midx_hash = hashlib.new(hash_name, midx_data[:-hl]).digest()
    assert trailer_midx == computed_midx_hash, "MIDX trailer checksum mismatch"
    assert num_base == 0, "write emits a base count of 0"

    toc, chunks = read_toc(midx_data, 12, num_chunks, hl)
    midx_chunks = dict(toc)
    assert b"LOFF" not in chunks, "lab expects no large-offset chunk"

    print(
        f"MIDX Header: signature=MIDX version={ver} oid_version={oid_ver} "
        f"chunks={num_chunks} base_midx={num_base} packs={num_packs}"
    )
    print("MIDX Chunk Table of Contents:")
    print_toc(toc)

    # PNAM: Packfile Names (NUL terminated, padded to a 4-byte boundary)
    pack_names = [p.decode("ascii") for p in chunks[b"PNAM"].split(b"\x00") if p]
    assert len(pack_names) == num_packs, "PNAM count must equal the header count"
    assert pack_names == sorted(pack_names), "PNAM must be in lexical order"
    assert len(chunks[b"PNAM"]) % 4 == 0, "PNAM must be padded to 4 bytes"
    for name in pack_names:
        assert os.path.exists(os.path.join(pack_dir, name)), f"{name} missing"
    print(f"Indexed Packfiles ({len(pack_names)}):")
    for i, p in enumerate(pack_names):
        print(f"  pack [{i}]: {p}")

    # OIDF & OIDL: Object Fanout and Lookup
    oidf = struct.unpack(">256I", chunks[b"OIDF"])
    total_objects = oidf[255]
    print(f"Total Objects Indexed: {total_objects}")

    oidl = chunks[b"OIDL"]
    ooff = chunks[b"OOFF"]
    assert len(oidl) == total_objects * hl, "OIDL length"
    assert len(ooff) == total_objects * 8, "OOFF length"
    oid_list = [oidl[i * hl : (i + 1) * hl] for i in range(total_objects)]
    check_fanout(oidf, oid_list)
    objects: list[tuple[str, int, int]] = []
    for i in range(total_objects):
        p_id, off = struct.unpack(">II", ooff[i * 8 : (i + 1) * 8])
        assert p_id < num_packs and off < 0x80000000, "OOFF entry out of range"
        objects.append((oid_list[i].hex(), p_id, off))

    # Cross-check every entry against the .idx files that git wrote.
    per_pack: dict[int, dict[str, int]] = {}
    for p_id, name in enumerate(pack_names):
        with open(os.path.join(pack_dir, name), "rb") as idx:
            shown = git(repo_dir, "show-index", stdin=idx.read())
        per_pack[p_id] = {
            line.split()[1]: int(line.split()[0]) for line in shown.splitlines()
        }
    assert sum(len(v) for v in per_pack.values()) == total_objects, "packs overlap"
    for oid, p_id, off in objects:
        assert per_pack[p_id].get(oid) == off, f"OOFF disagrees with idx for {oid}"
    stored = set(git(repo_dir, "cat-file", "--batch-all-objects",
                     "--batch-check=%(objectname)").split())
    assert stored == {o[0] for o in objects}, "MIDX must index every object"

    print("MIDX Object Lookup Samples:")
    sample_indices = [0, total_objects // 2, total_objects - 1]
    for idx in sample_indices:
        oid, p_id, off = objects[idx]
        print(f"  pos {idx:2d}: oid={oid} pack_id={p_id} pack_offset={off}")

    # BTMP: Bitmapped Packfiles
    btmp_data = chunks[b"BTMP"]
    assert len(btmp_data) == num_packs * 8, "BTMP length"
    print("Bitmapped Packfile Pseudo-Pack Intervals (BTMP):")
    intervals: list[tuple[int, int]] = []
    expected_pos = 0
    for i in range(num_packs):
        bpos, bnr = struct.unpack(">II", btmp_data[i * 8 : (i + 1) * 8])
        assert bpos == expected_pos, f"pack {i} interval is not contiguous"
        assert bnr == len(per_pack[i]), f"pack {i} interval size != its object count"
        expected_pos += bnr
        intervals.append((bpos, bnr))
        print(
            f"  pack [{i}]: bitmap_pos={bpos:2d} bitmap_nr={bnr:2d} "
            f"range=[{bpos:2d}, {bpos+bnr:2d})"
        )
    assert expected_pos == total_objects, "Total bitmapped objects mismatch"
    if preferred is not None:
        assert intervals[pack_names.index(preferred)][0] == 0, (
            "the preferred pack must occupy bit position 0"
        )

    # RIDX: pseudo-pack position -> MIDX position, in pack order inside each interval
    ridx_data = chunks[b"RIDX"]
    ridx_entries = struct.unpack(f">{total_objects}I", ridx_data)
    assert sorted(ridx_entries) == list(range(total_objects)), "RIDX is a permutation"
    for p_id, (bpos, bnr) in enumerate(intervals):
        offs = []
        for j in range(bpos, bpos + bnr):
            _, owner, off = objects[ridx_entries[j]]
            assert owner == p_id, f"RIDX[{j}] is not an object of pack {p_id}"
            offs.append(off)
        assert offs == sorted(offs), f"pack {p_id} interval is not in pack order"
    print(f"Reverse Index (RIDX): {len(ridx_entries)} pseudo-pack mappings validated")


def inspect_commit_graph(repo_dir: str) -> None:
    info_dir = os.path.join(repo_dir, ".git", "objects", "info")
    cg_file = os.path.join(info_dir, "commit-graph")
    if not os.path.exists(cg_file):
        print(f"Error: {cg_file} not found", file=sys.stderr)
        sys.exit(1)

    with open(cg_file, "rb") as f:
        cg_data = f.read()

    assert cg_data[:4] == b"CGPH", "Invalid Commit-Graph signature"
    cg_ver, cg_hash_ver, cg_chunks_count, cg_base = struct.unpack(
        ">BBBB", cg_data[4:8]
    )
    assert cg_ver == 1 and cg_hash_ver in HASHES, "unsupported commit-graph header"
    hash_name, hl = HASHES[cg_hash_ver]
    trailer_cg = cg_data[-hl:]
    computed_cg_hash = hashlib.new(hash_name, cg_data[:-hl]).digest()
    assert trailer_cg == computed_cg_hash, "Commit-Graph trailer checksum mismatch"
    assert cg_base == 0, "lab expects a single (non-split) commit-graph"

    toc, chunks = read_toc(cg_data, 8, cg_chunks_count, hl)
    print(
        f"\nCommit-Graph Header: signature=CGPH version={cg_ver} "
        f"hash_version={cg_hash_ver} chunks={cg_chunks_count} base_graphs={cg_base}"
    )
    print("Commit-Graph Chunk Table of Contents:")
    print_toc(toc)

    cg_oidf = struct.unpack(">256I", chunks[b"OIDF"])
    num_commits = cg_oidf[255]
    print(f"Total Commits Indexed: {num_commits}")

    cg_oidl = chunks[b"OIDL"]
    assert len(cg_oidl) == num_commits * hl, "OIDL length"
    oid_bytes = [cg_oidl[i * hl : (i + 1) * hl] for i in range(num_commits)]
    check_fanout(cg_oidf, oid_bytes)
    cg_oids = [o.hex() for o in oid_bytes]
    entry_len = hl + 16
    cdat = chunks[b"CDAT"]
    gda2 = chunks[b"GDA2"]
    assert len(cdat) == num_commits * entry_len, "CDAT length"
    assert len(gda2) == num_commits * 4, "GDA2 length"
    assert b"EDGE" not in chunks, "lab expects no octopus merge"

    # What git itself says about the same commits.
    truth: dict[str, tuple[str, int, list[str]]] = {}
    for line in git(repo_dir, "log", "--all", "--format=%H %T %ct %P").splitlines():
        fields = line.split()
        truth[fields[0]] = (fields[1], int(fields[2]), fields[3:])
    assert set(truth) == set(cg_oids), "commit-graph must hold every commit"

    def parents_of(oid: str) -> list[str]:
        return truth[oid][2]

    gen_v1_expect: dict[str, int] = {}
    corrected_expect: dict[str, int] = {}

    def solve(oid: str) -> None:
        if oid in gen_v1_expect:
            return
        for par in parents_of(oid):
            solve(par)
        pars = parents_of(oid)
        gen_v1_expect[oid] = 1 + max((gen_v1_expect[p] for p in pars), default=0)
        corrected_expect[oid] = max(
            [truth[oid][1]] + [corrected_expect[p] + 1 for p in pars]
        )

    for oid in cg_oids:
        solve(oid)

    print("Commit Data and Generation Numbers (CDAT & GDA2):")
    gen_v2_of: dict[str, int] = {}
    for i in range(num_commits):
        entry = cdat[i * entry_len : (i + 1) * entry_len]
        tree = entry[:hl].hex()
        p1, p2 = struct.unpack(">II", entry[hl : hl + 8])
        gen_time = struct.unpack(">II", entry[hl + 8 : hl + 16])
        gen_v1 = (gen_time[0] >> 2) & 0x3FFFFFFF
        cdate = ((gen_time[0] & 0x3) << 32) | gen_time[1]
        cdate_offset = struct.unpack(">I", gda2[i * 4 : (i + 1) * 4])[0]
        assert not cdate_offset & EDGE_FLAG, "lab expects no GDO2 overflow"
        gen_v2 = cdate + cdate_offset
        oid = cg_oids[i]
        gen_v2_of[oid] = gen_v2

        assert not p2 & EDGE_FLAG, "lab expects at most two parents"
        decoded = [cg_oids[p] for p in (p1, p2) if p != NULL_PARENT]
        assert decoded == parents_of(oid), f"parent positions of {oid} disagree"
        assert tree == truth[oid][0], f"tree of {oid} disagrees with git"
        assert cdate == truth[oid][1], f"commit date of {oid} disagrees with git"
        assert gen_v1 == gen_v1_expect[oid], f"gen_v1 of {oid} != 1 + max(parents)"
        assert gen_v2 == corrected_expect[oid], f"gen_v2 of {oid} is not the corrected date"

        p1_str = f"pos={p1}" if p1 != NULL_PARENT else "none"
        p2_str = f"pos={p2}" if p2 != NULL_PARENT else "none"
        print(
            f"  commit [{i}] {cg_oids[i]}: tree={tree[:8]} "
            f"parents=({p1_str}, {p2_str}) gen_v1={gen_v1} gen_v2={gen_v2}"
        )
    for oid, pars in ((o, parents_of(o)) for o in cg_oids):
        for par in pars:
            assert gen_v2_of[oid] > gen_v2_of[par], "corrected dates must grow along edges"

    # Bloom Filter Data
    bidx_data = chunks[b"BIDX"]
    bdat_data = chunks[b"BDAT"]
    bloom_ver, bloom_hashes, bloom_bits = struct.unpack(">III", bdat_data[:12])
    assert len(bidx_data) == num_commits * 4, "BIDX length"
    bidx = struct.unpack(f">{num_commits}I", bidx_data)
    assert list(bidx) == sorted(bidx), "BIDX offsets must not decrease"
    assert bidx[-1] == len(bdat_data) - 12, "last BIDX offset must end BDAT"
    assert bloom_ver in (1, 2) and bloom_hashes > 0 and bloom_bits > 0
    # Every path git reports as changed against the first parent must be in the filter.
    for i, oid in enumerate(cg_oids):
        start = bidx[i - 1] if i else 0
        filt = bdat_data[12 + start : 12 + bidx[i]]
        first_parent = parents_of(oid)[:1]
        if first_parent:
            paths = git(repo_dir, "diff", "--name-only", first_parent[0], oid).split()
        else:
            paths = git(repo_dir, "diff-tree", "--no-commit-id", "--name-only",
                        "-r", "--root", oid).split()
        assert paths, f"commit {oid} unexpectedly changes nothing"
        for path in paths:
            assert "/" not in path, "lab models flat paths only"
            assert bloom_contains(filt, path.encode("ascii"), bloom_hashes), (
                f"Bloom filter of {oid} misses {path}"
            )
        assert not bloom_contains(filt, b"absent.c", bloom_hashes), (
            f"Bloom filter of {oid} claims a path that never changed"
        )
    print(
        f"\nBloom Filters (BDAT): version={bloom_ver} hash_passes={bloom_hashes} "
        f"bits_per_entry={bloom_bits}"
    )
    print(f"Bloom Filter Index Offsets: {list(bidx)}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: midx_inspect.py <repo_dir> [preferred_idx_name]", file=sys.stderr)
        sys.exit(1)

    repo_dir = sys.argv[1]
    preferred = sys.argv[2] if len(sys.argv) > 2 else None

    inspect_midx(repo_dir, preferred)
    inspect_commit_graph(repo_dir)

    # Verification via Git plumbing
    p_midx = subprocess.run(
        ["git", "multi-pack-index", "verify"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    p_cg = subprocess.run(
        ["git", "commit-graph", "verify"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_midx.returncode == 0, f"git multi-pack-index verify failed: {p_midx.stderr}"
    assert p_cg.returncode == 0, f"git commit-graph verify failed: {p_cg.stderr}"

    print("\nVerification Status:")
    print("  multi-pack-index trailer SHA-1: VALID")
    print("  commit-graph trailer SHA-1: VALID")
    print("  git multi-pack-index verify: PASS (0 errors)")
    print("  git commit-graph verify: PASS (0 errors)")


if __name__ == "__main__":
    main()
