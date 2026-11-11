#!/usr/bin/env python3
"""Test and verification driver for MiniGit.

Every PASS line below is printed only after a check that can fail.
The checks avoid reusing the writer's own formula where a second, independent computation exists:
closed-form padding, hand-built byte vectors, well-known object IDs, and a parser of the on-disk
bytes written in this file.
The lab never runs the `git` binary, so it proves self-consistency and the documented formats,
not compatibility with `git fsck`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import minigit  # noqa: E402

# Object IDs every Git user can reproduce with `git hash-object` and `git write-tree`.
HELLO_OID = "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"  # blob "hello world\n"
EMPTY_BLOB_OID = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def expect_error(fn, exc=(ValueError, KeyError, zlib.error)) -> str:
    """Run fn and return the error text; fail the lab if fn does not raise."""
    try:
        fn()
    except exc as e:
        return str(e)
    raise AssertionError("expected an error, but the call succeeded")


def vi(n: int) -> bytes:
    """Little-endian base-128 varint used by the delta header (written independently of minigit)."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def decode_ops(delta: bytes):
    """Decode a delta into (base_size, target_size, ops); ops are ('copy', offset, size) or ('insert', n)."""
    pos = 0

    def varint():
        nonlocal pos
        res = shift = 0
        while True:
            b = delta[pos]
            pos += 1
            res |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return res

    base_size = varint()
    target_size = varint()
    ops = []
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            off = size = 0
            for bit in range(4):
                if op & (1 << bit):
                    off |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            ops.append(("copy", off, size or 0x10000))
        else:
            ops.append(("insert", op))
            pos += op
    return base_size, target_size, ops


def describe_ops(ops) -> str:
    return ", ".join(f"copy {o[2]}B@{o[1]}" if o[0] == "copy" else f"insert {o[1]}B" for o in ops)


def filler_bytes(n: int, seed: bytes) -> bytes:
    """Deterministic, effectively incompressible bytes."""
    out = bytearray()
    block = seed
    while len(out) < n:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:n])


def make_entry(path: str, payload: bytes, oid: str, mode: int = 0o100644, ino: int = 100) -> minigit.IndexEntry:
    return minigit.IndexEntry(
        ctime_s=1700000000, ctime_ns=0, mtime_s=1700000000, mtime_ns=0,
        dev=1, ino=ino, mode=mode, uid=501, gid=20,
        file_size=len(payload), sha1=bytes.fromhex(oid),
        flags=len(path.encode("utf-8")), path=path,
    )


def parse_idx(idx: bytes):
    """Parse a pack index v2 independently: returns (fanout, oids, crcs, offsets)."""
    assert idx[:8] == b"\xfftOc" + struct.pack(">I", 2)
    fanout = struct.unpack(">256I", idx[8:1032])
    n = fanout[255]
    oids = [idx[1032 + 20 * i: 1052 + 20 * i] for i in range(n)]
    base = 1032 + 20 * n
    crcs = list(struct.unpack(f">{n}I", idx[base: base + 4 * n]))
    offsets = list(struct.unpack(f">{n}I", idx[base + 4 * n: base + 8 * n]))
    return fanout, oids, crcs, offsets


def main() -> None:
    tmp_root = tempfile.mkdtemp(prefix="minigit_lab_")
    try:
        repo_dir = os.path.join(tmp_root, "repo")
        checkout_dir = os.path.join(tmp_root, "checkout")
        rust_checkout_dir = os.path.join(tmp_root, "checkout_rs")

        # ------------------------------------------------------------------ Step 1
        print("=== Step 1: Content-Addressable Storage (CAS) Loose Objects ===")
        minigit.init_repo(repo_dir)
        print(f"Initialized repository in {repo_dir}")

        blob_payload = b"hello world\n"
        blob_oid = minigit.hash_object(repo_dir, blob_payload, obj_type="blob", write=True)
        assert blob_oid == HELLO_OID, blob_oid
        print(f"Stored blob \"hello world\\n\": oid={blob_oid} size={len(blob_payload)}")

        obj_rel_path = os.path.join(".git", "objects", blob_oid[:2], blob_oid[2:])
        obj_abs_path = os.path.join(repo_dir, obj_rel_path)
        assert os.path.exists(obj_abs_path)
        print(f"Loose object path exists: {obj_rel_path}")

        obj_type, read_payload = minigit.read_loose_object(repo_dir, blob_oid)
        assert obj_type == "blob"
        assert read_payload == blob_payload
        print(f"Read loose object: type={obj_type} size={len(read_payload)} payload=\"{read_payload.decode('utf-8').strip()}\"")

        with open(obj_abs_path, "rb") as f:
            raw = zlib.decompress(f.read())
        assert raw == b"blob 12\x00hello world\n"
        assert hashlib.sha1(raw).hexdigest() == blob_oid
        assert minigit.hash_object(repo_dir, b"", "blob", write=False) == EMPTY_BLOB_OID
        assert minigit.write_tree_from_index(os.path.join(tmp_root, "empty"), []) == EMPTY_TREE_OID
        # A loose object stored under the wrong name must be rejected on read.
        wrong = "00" + "0" * 38
        os.makedirs(os.path.join(repo_dir, ".git", "objects", "00"), exist_ok=True)
        shutil.copyfile(obj_abs_path, os.path.join(repo_dir, ".git", "objects", "00", wrong[2:]))
        expect_error(lambda: minigit.read_loose_object(repo_dir, wrong))
        os.remove(os.path.join(repo_dir, ".git", "objects", "00", wrong[2:]))
        print("SHA-1 verification: PASS")

        # ------------------------------------------------------------------ Step 2
        print("\n=== Step 2: Binary Index Parsing and Serialization (Index v2) ===")
        src_payload = b"int main() { return 0; }\n"
        src_oid = minigit.hash_object(repo_dir, src_payload, obj_type="blob", write=True)
        script_payload = b"#!/bin/sh\necho building...\n"
        script_oid = minigit.hash_object(repo_dir, script_payload, obj_type="blob", write=True)

        e1 = make_entry("hello.txt", blob_payload, blob_oid, ino=100)
        e2 = make_entry("src/main.c", src_payload, src_oid, ino=101)
        e3 = make_entry("build.sh", script_payload, script_oid, mode=0o100755, ino=102)
        # A file whose name is "src" plus a dot: with the virtual slash it sorts before the directory "src".
        sibling_payload = b"a plain file next to the src directory\n"
        sibling_oid = minigit.hash_object(repo_dir, sibling_payload, obj_type="blob", write=True)
        e4 = make_entry("src.txt", sibling_payload, sibling_oid, ino=103)

        minigit.write_index(repo_dir, [e1, e2, e3, e4])
        loaded = minigit.read_index(repo_dir)
        assert len(loaded) == 4
        print(f"Staged {len(loaded)} files:")
        for idx, e in enumerate(loaded):
            print(f"  [{idx}] mode={e.mode:o} size={e.file_size} path={e.path} oid={e.oid_hex}")

        with open(os.path.join(repo_dir, ".git", "index"), "rb") as f:
            index_data = f.read()
        assert index_data[:12] == b"DIRC" + struct.pack(">II", 2, 4)
        # Walk the bytes using the path length from the flags field and the closed form
        # "next multiple of 8 strictly above the unpadded length", not the writer's formula.
        pos = 12
        offsets = [pos]
        for e in loaded:
            path_len = struct.unpack(">H", index_data[pos + 60: pos + 62])[0] & 0xFFF
            assert path_len == len(e.path)
            assert index_data[pos + 62: pos + 62 + path_len] == e.path.encode()
            unpadded = 62 + path_len
            total = (unpadded // 8 + 1) * 8
            assert index_data[pos + unpadded: pos + total] == b"\x00" * (total - unpadded)
            assert 1 <= total - unpadded <= 8
            pos += total
            offsets.append(pos)
        assert pos + 20 == len(index_data), "entries must end exactly where the 20-byte trailer starts"
        print(f"Serialized index: header=DIRC version=2 entries={len(loaded)}")
        print(f"Index 8-byte entry alignment: PASS (entry offsets: {offsets})")

        assert hashlib.sha1(index_data[:-20]).digest() == index_data[-20:]
        bad_trailer = bytearray(index_data)
        bad_trailer[-1] ^= 0xFF
        bad_body = bytearray(index_data)
        bad_body[20] ^= 0x01
        probe_repo = os.path.join(tmp_root, "probe")
        minigit.init_repo(probe_repo)
        for blob in (bad_trailer, bad_body):
            with open(os.path.join(probe_repo, ".git", "index"), "wb") as f:
                f.write(blob)
            expect_error(lambda: minigit.read_index(probe_repo))
        print("Index trailer SHA-1 checksum: PASS (corrupt trailer and corrupt body both rejected)")

        # Boundary lengths: padding must be 1 to 8 NUL bytes, 8 when 62 + len is already a multiple of 8.
        for path_len in range(1, 20):
            path = "x" * path_len
            minigit.write_index(probe_repo, [make_entry(path, b"", EMPTY_BLOB_OID)])
            with open(os.path.join(probe_repo, ".git", "index"), "rb") as f:
                data = f.read()
            expected_total = ((62 + path_len) // 8 + 1) * 8
            assert len(data) == 12 + expected_total + 20, (path_len, len(data))
            assert [e.path for e in minigit.read_index(probe_repo)] == [path]
        assert (62 + 2) % 8 == 0
        print("Index boundary: a 2-byte path (62 + 2 = 64) takes 8 NUL bytes, entry 72 bytes: PASS")

        # ------------------------------------------------------------------ Step 3
        print("\n=== Step 3: Hierarchical Tree Traversal and Slash-Sort Ordering ===")
        print("Testing canonical Git directory sorting (base_name_compare):")
        raw_names = [("a.c", 0o100644), ("a-b", 0o100644), ("a0", 0o100644), ("a", 0o040000)]
        print(f"Input unsorted names: {['a.c', 'a-b', 'a0', 'a/']}")
        sorted_names = sorted(raw_names, key=lambda x: minigit.base_name_key(x[0], x[1]))
        print("Sorted tree entries (directories end with /):")
        for name, mode in sorted_names:
            print(f"  {mode:06o} {'tree' if mode == 0o040000 else 'blob'} {name}")
        assert [x[0] for x in sorted_names] == ["a-b", "a.c", "a", "a0"]

        # The same four names through the real tree writer, then read back from the stored bytes.
        sort_repo = os.path.join(tmp_root, "sortrepo")
        minigit.init_repo(sort_repo)
        sort_entries = [
            make_entry(p, b"x", minigit.hash_object(sort_repo, b"x", "blob", write=True))
            for p in ("a.c", "a-b", "a0", "a/file.txt")
        ]
        sort_tree = minigit.write_tree_from_index(sort_repo, sort_entries)
        _, sort_raw = minigit.read_loose_object(sort_repo, sort_tree)
        stored_names = []
        p = 0
        while p < len(sort_raw):
            sp = sort_raw.index(b" ", p)
            nul = sort_raw.index(b"\x00", sp)
            stored_names.append((sort_raw[p:sp].decode(), sort_raw[sp + 1: nul].decode()))
            p = nul + 21
        assert stored_names == [("100644", "a-b"), ("100644", "a.c"), ("40000", "a"), ("100644", "a0")], stored_names
        print("Tree sorting matches Git canonical order: PASS (the stored tree bytes list a-b, a.c, a, a0)")

        root_tree_oid = minigit.write_tree_from_index(repo_dir, loaded)
        print(f"Built root tree: oid={root_tree_oid}")

        print("Tree recursive traversal:")
        _, root_tree_raw = minigit.read_loose_object(repo_dir, root_tree_oid)
        root_items = minigit.parse_tree_object(root_tree_raw)
        walked = []
        for mode, item_type, oid, name in root_items:
            print(f"  {mode:06o} {item_type} {oid} {name}")
            walked.append(name)
            if item_type == "tree":
                _, sub_raw = minigit.read_loose_object(repo_dir, oid)
                for sm, st, so, sn in minigit.parse_tree_object(sub_raw):
                    print(f"  {sm:06o} {st} {so} {name}/{sn}")
                    walked.append(f"{name}/{sn}")
        assert walked == ["build.sh", "hello.txt", "src.txt", "src", "src/main.c"]
        assert [(i[0], i[2]) for i in root_items[:2]] == [(0o100755, script_oid), (0o100644, blob_oid)]

        # ------------------------------------------------------------------ Step 4
        print("\n=== Step 4: Packfile Decompression and OFS_DELTA Reconstruction ===")
        # Known vectors for the biased OFS_DELTA offset encoding (no bias would give 0x80 0x00 -> 0 and so on).
        for dist, enc in ((127, "7f"), (128, "8000"), (129, "8001"), (16511, "ff7f"), (16512, "808000")):
            assert minigit.encode_ofs_delta_offset(dist).hex() == enc, (dist, enc)
            assert minigit.decode_ofs_delta_offset(bytes.fromhex(enc), 0) == (dist, len(enc) // 2), (dist, enc)

        base_text = b"line 1: initial version\nline 2: stable config\nline 3: ending\n"
        target_text = b"line 1: initial version\nline 2: modified config for test\nline 3: ending\n"
        filler = filler_bytes(200, b"filler")
        big_base = filler_bytes(0x10000, b"big-base")
        big_target_a = big_base + b"tail\n"
        big_target_b = big_base[0x0A1B: 0x0A1B + 0x0123] + b"end\n"

        def blob(data: bytes) -> str:
            return minigit.hash_object(repo_dir, data, "blob", write=True)

        base_oid, target_oid, filler_oid = blob(base_text), blob(target_text), blob(filler)
        big_base_oid, big_a_oid, big_b_oid = blob(big_base), blob(big_target_a), blob(big_target_b)

        delta_small = minigit.create_delta(base_text, target_text)
        # Hand-built deltas: copy with the size bytes absent (means 0x10000), and a copy that needs
        # two offset bytes and two size bytes (opcode 0xB3).
        delta_a = vi(len(big_base)) + vi(len(big_target_a)) + bytes([0x80]) + bytes([5]) + b"tail\n"
        delta_b = (vi(len(big_base)) + vi(len(big_target_b))
                   + bytes([0xB3, 0x1B, 0x0A, 0x23, 0x01]) + bytes([4]) + b"end\n")
        assert minigit.apply_delta(big_base, delta_a) == big_target_a
        assert minigit.apply_delta(big_base, delta_b) == big_target_b

        pack_entries = [
            (base_oid, minigit.OBJ_BLOB, base_text, None),
            (filler_oid, minigit.OBJ_BLOB, filler, None),
            (target_oid, minigit.OBJ_BLOB, target_text, ("ofs", 0, delta_small)),
            (big_base_oid, minigit.OBJ_BLOB, big_base, None),
            (big_a_oid, minigit.OBJ_BLOB, big_target_a, ("ofs", 3, delta_a)),
            (big_b_oid, minigit.OBJ_BLOB, big_target_b, ("ofs", 3, delta_b)),
        ]
        pack_p, idx_p = minigit.create_packfile(repo_dir, pack_entries)
        with open(pack_p, "rb") as f:
            pack_data = f.read()
        with open(idx_p, "rb") as f:
            idx_data = f.read()

        magic, version, count = struct.unpack(">4sII", pack_data[:12])
        assert (magic, version, count) == (b"PACK", 2, len(pack_entries))
        print(f"Created packfile with {count} objects (3 undeltified blobs, 3 OFS_DELTA blobs):")
        print(f"Packfile header: magic={magic.decode()} version={version} count={count}")

        fanout, idx_oids, idx_crcs, idx_offsets = parse_idx(idx_data)
        oid_to_offset = {o.hex(): off for o, off in zip(idx_oids, idx_offsets)}
        oid_to_crc = {o.hex(): c for o, c in zip(idx_oids, idx_crcs)}

        def entry_header(offset: int):
            """Return (type, size, position after the header) of the pack entry at offset."""
            b = pack_data[offset]
            pos = offset + 1
            type_id, size, shift = (b >> 4) & 7, b & 0x0F, 4
            while b & 0x80:
                b = pack_data[pos]
                pos += 1
                size |= (b & 0x7F) << shift
                shift += 7
            return type_id, size, pos

        base_type, base_size, _ = entry_header(oid_to_offset[base_oid])
        assert (base_type, base_size, oid_to_offset[base_oid]) == (minigit.OBJ_BLOB, len(base_text), 12)
        print(f"Base entry at pack offset {oid_to_offset[base_oid]}: type=OBJ_BLOB size={base_size}")

        def ofs_target(oid: str):
            """Parse a delta entry's header from the pack bytes; return (delta size, ofs byte count, base offset)."""
            off = oid_to_offset[oid]
            type_id, dsize, pos = entry_header(off)
            assert type_id == minigit.OBJ_OFS_DELTA
            c = pack_data[pos]
            start = pos
            pos += 1
            dist = c & 0x7F
            while c & 0x80:
                c = pack_data[pos]
                pos += 1
                dist = ((dist + 1) << 7) | (c & 0x7F)
            return dsize, pos - start, off - dist

        dsize, ofs_len, base_off = ofs_target(target_oid)
        assert dsize == len(delta_small) and base_off == oid_to_offset[base_oid]
        # The filler blob sits between base and delta, so the distance does not fit in one byte.
        assert oid_to_offset[target_oid] - base_off >= 128 and ofs_len == 2
        print(f"Delta entry 1: type=OBJ_OFS_DELTA delta_size={dsize} ofs_bytes={ofs_len} base_offset={base_off}")
        bsz, tsz, ops = decode_ops(delta_small)
        assert (bsz, tsz) == (len(base_text), len(target_text))
        assert sum(o[2] if o[0] == "copy" else o[1] for o in ops) == len(target_text)
        print(f"Applying delta instructions: {describe_ops(ops)}")

        dsize_a, ofs_len_a, base_off_a = ofs_target(big_a_oid)
        dsize_b, ofs_len_b, base_off_b = ofs_target(big_b_oid)
        assert base_off_a == base_off_b == oid_to_offset[big_base_oid]
        assert ofs_len_a == ofs_len_b == 3, "a 64 KiB base needs a three-byte biased offset"
        _, _, ops_a = decode_ops(delta_a)
        _, _, ops_b = decode_ops(delta_b)
        print(f"Delta entry 2: type=OBJ_OFS_DELTA delta_size={dsize_a} ofs_bytes={ofs_len_a} ops: {describe_ops(ops_a)}")
        print(f"Delta entry 3: type=OBJ_OFS_DELTA delta_size={dsize_b} ofs_bytes={ofs_len_b} ops: {describe_ops(ops_b)}")
        # The first op of delta_a is the bare opcode 0x80: no offset byte, no size byte, so the size is 0x10000.
        assert ops_a[0] == ("copy", 0, 0x10000)

        for oid, payload in ((target_oid, target_text), (big_a_oid, big_target_a), (big_b_oid, big_target_b),
                             (base_oid, base_text), (filler_oid, filler), (big_base_oid, big_base)):
            rec_type, rec_bytes = minigit.read_packfile_object(pack_p, idx_p, oid)
            assert rec_type == "blob" and rec_bytes == payload
            assert hashlib.sha1(f"blob {len(payload)}\x00".encode() + payload).hexdigest() == oid
        print("Target reconstructed payload matches: PASS")
        print("Target reconstructed SHA-1 matches: PASS")

        # Fanout: cumulative counts per first byte; and a missing object is reported as missing.
        assert all(fanout[i] <= fanout[i + 1] for i in range(255)) and fanout[255] == count
        for b in range(256):
            assert fanout[b] - (fanout[b - 1] if b else 0) == sum(1 for o in idx_oids if o[0] == b)
        assert idx_oids == sorted(idx_oids)
        missing = hashlib.sha1(b"not in the pack").hexdigest()
        expect_error(lambda: minigit.read_packfile_object(pack_p, idx_p, missing), KeyError)
        print("Index v2 fanout table binary search: PASS")

        # Trailers and per-entry CRC32 (computed here over the raw entry bytes, from the pack file).
        assert hashlib.sha1(pack_data[:-20]).digest() == pack_data[-20:]
        assert idx_data[-40:-20] == pack_data[-20:]
        assert hashlib.sha1(idx_data[:-20]).digest() == idx_data[-20:]
        ends = sorted(idx_offsets) + [len(pack_data) - 20]
        for oid, off in oid_to_offset.items():
            end = ends[ends.index(off) + 1]
            assert zlib.crc32(pack_data[off:end]) & 0xFFFFFFFF == oid_to_crc[oid]
        bad_pack = os.path.join(tmp_root, "bad.pack")
        # Flip a byte inside the filler blob, which reading target_oid never touches: only the pack trailer check can notice.
        for flip_at in (oid_to_offset[filler_oid] + 20, len(pack_data) - 1):
            mutated = bytearray(pack_data)
            mutated[flip_at] ^= 0x01
            with open(bad_pack, "wb") as f:
                f.write(mutated)
            expect_error(lambda: minigit.read_packfile_object(bad_pack, idx_p, target_oid))
        bad_idx = os.path.join(tmp_root, "bad.idx")
        mutated = bytearray(idx_data)
        mutated[1100] ^= 0x01
        with open(bad_idx, "wb") as f:
            f.write(mutated)
        expect_error(lambda: minigit.read_packfile_object(pack_p, bad_idx, target_oid))
        print("Packfile checksum and index checksum trailers: PASS (per-entry CRC32 and corrupt pack and index rejected)")

        # ------------------------------------------------------------------ Step 5
        print("\n=== Step 5: Commit Graph and Working Tree Checkout ===")
        commit_oid = minigit.commit_tree(
            repo_dir, root_tree_oid, message="Initial repository commit",
            author_name="Toolchain Engineer", author_email="engineer@example.com",
            timestamp=1700000000
        )
        print(f"Created initial commit: oid={commit_oid}")
        with open(os.path.join(repo_dir, ".git", "HEAD")) as f:
            head_val = f.read().strip()
        print(f"HEAD points to: {head_val}")
        branch_ref = head_val.split(" ")[1]
        with open(os.path.join(repo_dir, ".git", branch_ref)) as f:
            branch_val = f.read().strip()
        print(f"{branch_ref} points to: {branch_val}")
        assert branch_val == commit_oid
        c_type, c_raw = minigit.read_loose_object(repo_dir, commit_oid)
        assert c_type == "commit"
        assert c_raw.decode().split("\n")[0] == f"tree {root_tree_oid}"
        assert c_raw.endswith(b"\n\nInitial repository commit\n")

        print(f"Executing checkout to clean working directory {checkout_dir}:")
        restored = minigit.checkout_tree(repo_dir, root_tree_oid, checkout_dir)
        print("Restored files:")
        for e in sorted(restored, key=lambda x: x.path):
            on_disk = os.stat(os.path.join(checkout_dir, e.path)).st_mode & 0o777
            print(f"  {e.path} (mode={0o100000 | on_disk:o})")
        expected_files = {
            "hello.txt": (blob_payload, 0o644),
            "src/main.c": (src_payload, 0o644),
            "src.txt": (sibling_payload, 0o644),
            "build.sh": (script_payload, 0o755),
        }
        for rel, (payload, perm) in expected_files.items():
            full = os.path.join(checkout_dir, rel)
            with open(full, "rb") as f:
                assert f.read() == payload
            assert os.stat(full).st_mode & 0o777 == perm, rel
        print("Restored file contents verified: PASS")

        # The returned index entries must describe the files as they are on disk now.
        assert sorted(e.path for e in restored) == sorted(expected_files)
        for e in restored:
            st = os.lstat(os.path.join(checkout_dir, e.path))
            assert e.file_size == st.st_size == len(expected_files[e.path][0])
            assert e.mtime_s == st.st_mtime_ns // 10**9 and e.mtime_ns == st.st_mtime_ns % 10**9
            assert e.ctime_s == st.st_ctime_ns // 10**9 and e.ctime_ns == st.st_ctime_ns % 10**9
            assert e.ino == st.st_ino & 0xFFFFFFFF and e.dev == st.st_dev & 0xFFFFFFFF
            assert (e.uid, e.gid) == (st.st_uid, st.st_gid)
            assert e.oid_hex == hashlib.sha1(b"blob %d\x00" % st.st_size + expected_files[e.path][0]).hexdigest()
        restored_repo = os.path.join(tmp_root, "restored_repo")
        minigit.init_repo(restored_repo)
        minigit.write_index(restored_repo, restored)
        again = minigit.read_index(restored_repo)
        assert [(e.path, e.mode, e.file_size, e.sha1, e.mtime_s, e.mtime_ns, e.ino) for e in again] == \
               [(e.path, e.mode, e.file_size, e.sha1, e.mtime_s, e.mtime_ns, e.ino) for e in restored]
        print("Index stat cache initialized: PASS (entries match lstat of each restored file and survive an index round trip)")

        # ------------------------------------------------------------------ Step 6
        print("\n=== Step 6: Cross-Language Verification (Rust Engine) ===")
        print("Building and running Rust mini-git engine...")
        rust_dir = os.path.join(SCRIPT_DIR, "minigit_rs")
        build_res = subprocess.run(["cargo", "build", "--quiet", "--release"], cwd=rust_dir, capture_output=True, text=True)
        if build_res.returncode != 0:
            print("Rust build failed:", build_res.stderr)
            sys.exit(1)
        rust_bin = os.path.join(rust_dir, "target", "release", "minigit_rs")
        run_res = subprocess.run([
            rust_bin, "verify-all", repo_dir, pack_p, idx_p, blob_oid, root_tree_oid, rust_checkout_dir
        ], capture_output=True, text=True)
        if run_res.returncode != 0:
            print("Rust execution failed:", run_res.stderr)
            sys.exit(1)
        print(run_res.stdout.strip())
        print("=== Rebuild Lab Verification Complete ===")
    finally:
        shutil.rmtree(tmp_root)


if __name__ == "__main__":
    main()
