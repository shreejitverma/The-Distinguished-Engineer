#!/usr/bin/env python3
"""Miniature Git implementation in pure Python.

Implements the documented Git on-disk formats: Content-Addressable Storage (CAS), binary Index v2,
hierarchical tree sorting (base_name_compare), packfile v2 creation and reading
with OBJ_OFS_DELTA decompression, and tree checkout.
It is checked against the specs and its own readers, not against the `git` binary.
"""

from __future__ import annotations

import collections
import hashlib
import os
import stat
import struct
import sys
import zlib
from typing import List, Tuple, Optional, Dict, Any

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

TYPE_NAMES = {
    OBJ_COMMIT: "commit",
    OBJ_TREE: "tree",
    OBJ_BLOB: "blob",
    OBJ_TAG: "tag",
}
NAME_TO_TYPE = {v: k for k, v in TYPE_NAMES.items()}


class IndexEntry:
    def __init__(
        self,
        ctime_s: int,
        ctime_ns: int,
        mtime_s: int,
        mtime_ns: int,
        dev: int,
        ino: int,
        mode: int,
        uid: int,
        gid: int,
        file_size: int,
        sha1: bytes,
        flags: int,
        path: str,
    ) -> None:
        self.ctime_s = ctime_s
        self.ctime_ns = ctime_ns
        self.mtime_s = mtime_s
        self.mtime_ns = mtime_ns
        self.dev = dev
        self.ino = ino
        self.mode = mode
        self.uid = uid
        self.gid = gid
        self.file_size = file_size
        self.sha1 = sha1  # 20 bytes
        self.flags = flags
        self.path = path

    @property
    def oid_hex(self) -> str:
        return self.sha1.hex()

    def serialize(self) -> bytes:
        # Fixed 62-byte header:
        # 10 32-bit unsigned ints = 40 bytes
        # 20 bytes SHA-1
        # 1 16-bit unsigned int = 2 bytes
        header = struct.pack(
            ">10I20sH",
            self.ctime_s,
            self.ctime_ns,
            self.mtime_s,
            self.mtime_ns,
            self.dev,
            self.ino,
            self.mode,
            self.uid,
            self.gid,
            self.file_size,
            self.sha1,
            self.flags,
        )
        path_bytes = self.path.encode("utf-8")
        entry_len = 62 + len(path_bytes)
        # Pad with 1 to 8 NUL bytes to align to 8-byte boundary
        pad_len = 8 - (entry_len % 8)
        padding = b"\x00" * pad_len
        return header + path_bytes + padding


def init_repo(repo_dir: str) -> None:
    git_dir = os.path.join(repo_dir, ".git")
    os.makedirs(os.path.join(git_dir, "objects"), exist_ok=True)
    os.makedirs(os.path.join(git_dir, "refs", "heads"), exist_ok=True)
    head_file = os.path.join(git_dir, "HEAD")
    if not os.path.exists(head_file):
        with open(head_file, "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/main\n")


def hash_object(repo_dir: str, data: bytes, obj_type: str = "blob", write: bool = True) -> str:
    header = f"{obj_type} {len(data)}\x00".encode("utf-8")
    store = header + data
    sha = hashlib.sha1(store).hexdigest()
    if write:
        obj_dir = os.path.join(repo_dir, ".git", "objects", sha[:2])
        os.makedirs(obj_dir, exist_ok=True)
        obj_path = os.path.join(obj_dir, sha[2:])
        if not os.path.exists(obj_path):
            compressed = zlib.compress(store)
            with open(obj_path, "wb") as f:
                f.write(compressed)
    return sha


def read_loose_object(repo_dir: str, oid: str) -> Tuple[str, bytes]:
    obj_path = os.path.join(repo_dir, ".git", "objects", oid[:2], oid[2:])
    if not os.path.exists(obj_path):
        raise FileNotFoundError(f"Loose object {oid} not found at {obj_path}")
    with open(obj_path, "rb") as f:
        raw = zlib.decompress(f.read())
    nul_pos = raw.find(b"\x00")
    if nul_pos == -1:
        raise ValueError(f"Corrupt loose object {oid}: missing null byte")
    header = raw[:nul_pos].decode("utf-8")
    obj_type, size_str = header.split(" ", 1)
    expected_size = int(size_str)
    body = raw[nul_pos + 1 :]
    if len(body) != expected_size:
        raise ValueError(f"Corrupt object {oid}: size mismatch {len(body)} != {expected_size}")
    actual_oid = hashlib.sha1(raw).hexdigest()
    if actual_oid != oid:
        raise ValueError(f"Corrupt object {oid}: content hashes to {actual_oid}")
    return obj_type, body


def read_index(repo_dir: str) -> List[IndexEntry]:
    index_path = os.path.join(repo_dir, ".git", "index")
    if not os.path.exists(index_path):
        return []
    with open(index_path, "rb") as f:
        data = f.read()
    if len(data) < 32:  # 12 header + 20 trailer
        raise ValueError("Index file too small")
    magic, version, count = struct.unpack(">4sII", data[:12])
    if magic != b"DIRC":
        raise ValueError(f"Invalid index signature: {magic!r}")
    if version != 2:
        raise ValueError(f"Unsupported index version: {version}")

    # Verify trailer checksum
    file_checksum = data[-20:]
    calc_checksum = hashlib.sha1(data[:-20]).digest()
    if file_checksum != calc_checksum:
        raise ValueError("Index trailer SHA-1 mismatch")

    entries: List[IndexEntry] = []
    pos = 12
    for _ in range(count):
        entry_start = pos
        fields = struct.unpack(">10I20sH", data[pos : pos + 62])
        pos += 62
        nul_idx = data.find(b"\x00", pos)
        if nul_idx == -1:
            raise ValueError("Unterminated path in index entry")
        path_str = data[pos:nul_idx].decode("utf-8")
        entry_raw_len = 62 + len(path_str.encode("utf-8"))
        pad_len = 8 - (entry_raw_len % 8)
        pos = entry_start + entry_raw_len + pad_len
        entry = IndexEntry(
            ctime_s=fields[0],
            ctime_ns=fields[1],
            mtime_s=fields[2],
            mtime_ns=fields[3],
            dev=fields[4],
            ino=fields[5],
            mode=fields[6],
            uid=fields[7],
            gid=fields[8],
            file_size=fields[9],
            sha1=fields[10],
            flags=fields[11],
            path=path_str,
        )
        entries.append(entry)
    return entries


def write_index(repo_dir: str, entries: List[IndexEntry]) -> None:
    index_path = os.path.join(repo_dir, ".git", "index")
    entries_sorted = sorted(entries, key=lambda e: e.path)
    header = struct.pack(">4sII", b"DIRC", 2, len(entries_sorted))
    payload = bytearray(header)
    for entry in entries_sorted:
        payload.extend(entry.serialize())
    trailer = hashlib.sha1(payload).digest()
    payload.extend(trailer)
    with open(index_path, "wb") as f:
        f.write(payload)


def base_name_key(name: str, mode: int) -> str:
    """Git canonical tree sorting key: directories sort as if ending with '/'."""
    is_dir = (mode == 0o040000) or stat.S_ISDIR(mode)
    return (name + "/") if is_dir else name


def write_tree_from_index(repo_dir: str, entries: List[IndexEntry]) -> str:
    """Builds Git tree hierarchy from index entries using canonical slash-sorting."""
    # Group by directory structure
    root_node: Dict[str, Any] = {}

    for e in entries:
        parts = e.path.split("/")
        curr = root_node
        for part in parts[:-1]:
            curr = curr.setdefault(part, {})
        curr[parts[-1]] = (e.mode, e.sha1)

    def write_subtree(node: Dict[str, Any]) -> Tuple[int, bytes, str]:
        # returns (mode, 20_bytes_sha1, hex_oid)
        items = []
        for name, value in node.items():
            if isinstance(value, dict):
                sub_mode, sub_sha1, _ = write_subtree(value)
                items.append((name, 0o040000, sub_sha1))
            else:
                mode, file_sha1 = value
                items.append((name, mode, file_sha1))

        # Sort with base_name_key
        items.sort(key=lambda x: base_name_key(x[0], x[1]))

        tree_bytes = bytearray()
        for name, mode, entry_sha1 in items:
            # Mode in octal without leading zero (e.g. "100644", "100755", "40000")
            mode_str = f"{mode:o}".encode("utf-8")
            tree_bytes.extend(mode_str)
            tree_bytes.append(0x20)
            tree_bytes.extend(name.encode("utf-8"))
            tree_bytes.append(0x00)
            tree_bytes.extend(entry_sha1)

        tree_oid = hash_object(repo_dir, bytes(tree_bytes), obj_type="tree", write=True)
        return 0o040000, bytes.fromhex(tree_oid), tree_oid

    _, _, root_tree_oid = write_subtree(root_node)
    return root_tree_oid


def parse_tree_object(data: bytes) -> List[Tuple[int, str, str, str]]:
    """Parses raw tree object payload into list of (mode, type, hex_oid, name)."""
    entries = []
    pos = 0
    while pos < len(data):
        space_pos = data.find(b" ", pos)
        if space_pos == -1:
            break
        mode_oct = int(data[pos:space_pos].decode("utf-8"), 8)
        nul_pos = data.find(b"\x00", space_pos)
        name = data[space_pos + 1 : nul_pos].decode("utf-8")
        sha1_bytes = data[nul_pos + 1 : nul_pos + 21]
        pos = nul_pos + 21
        obj_type = "tree" if mode_oct == 0o040000 else "blob"
        entries.append((mode_oct, obj_type, sha1_bytes.hex(), name))
    return entries


def commit_tree(
    repo_dir: str,
    tree_oid: str,
    parent_oid: Optional[str] = None,
    message: str = "initial commit",
    author_name: str = "MiniGit Author",
    author_email: str = "author@example.com",
    timestamp: int = 1700000000,
) -> str:
    lines = [f"tree {tree_oid}"]
    if parent_oid:
        lines.append(f"parent {parent_oid}")
    sig = f"{author_name} <{author_email}> {timestamp} +0000"
    lines.append(f"author {sig}")
    lines.append(f"committer {sig}")
    lines.append("")
    lines.append(message.strip())
    lines.append("")
    payload = "\n".join(lines).encode("utf-8")
    commit_oid = hash_object(repo_dir, payload, obj_type="commit", write=True)

    # Update ref
    head_path = os.path.join(repo_dir, ".git", "HEAD")
    target_ref = None
    if os.path.exists(head_path):
        with open(head_path, "r", encoding="utf-8") as f:
            head_val = f.read().strip()
        if head_val.startswith("ref: "):
            target_ref = head_val[5:]
    if not target_ref:
        target_ref = "refs/heads/main"
    ref_path = os.path.join(repo_dir, ".git", target_ref)
    os.makedirs(os.path.dirname(ref_path), exist_ok=True)
    with open(ref_path, "w", encoding="utf-8") as f:
        f.write(commit_oid + "\n")
    return commit_oid


def create_delta(base: bytes, target: bytes) -> bytes:
    """Generates standard Git delta stream between base and target."""
    def encode_vi(n: int) -> bytes:
        out = []
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        return bytes(out)

    delta = bytearray()
    delta.extend(encode_vi(len(base)))
    delta.extend(encode_vi(len(target)))

    chunk_size = 16 if len(base) >= 16 else 4
    base_map: Dict[bytes, int] = {}
    if len(base) >= chunk_size:
        for i in range(0, len(base) - chunk_size + 1):
            chunk = base[i : i + chunk_size]
            if chunk not in base_map:
                base_map[chunk] = i

    pos = 0
    pending_literal = bytearray()

    def flush_literal() -> None:
        nonlocal pending_literal
        p = 0
        while p < len(pending_literal):
            take = min(127, len(pending_literal) - p)
            delta.append(take)
            delta.extend(pending_literal[p : p + take])
            p += take
        pending_literal = bytearray()

    while pos < len(target):
        matched = False
        if pos + chunk_size <= len(target):
            chunk = target[pos : pos + chunk_size]
            if chunk in base_map:
                base_pos = base_map[chunk]
                match_len = 0
                while (
                    pos + match_len < len(target)
                    and base_pos + match_len < len(base)
                    and target[pos + match_len] == base[base_pos + match_len]
                ):
                    match_len += 1
                if match_len >= chunk_size:
                    flush_literal()
                    off = base_pos
                    sz = match_len
                    off_bytes = []
                    off_flag = 0
                    for i in range(4):
                        b = (off >> (i * 8)) & 0xFF
                        if b != 0 or (off >> (i * 8)) != 0:
                            off_bytes.append(b)
                            off_flag |= 1 << i
                    sz_bytes = []
                    sz_flag = 0
                    for i in range(3):
                        b = (sz >> (i * 8)) & 0xFF
                        if b != 0 or (sz >> (i * 8)) != 0:
                            sz_bytes.append(b)
                            sz_flag |= 1 << i
                    if not off_bytes:
                        off_bytes = [0]
                        off_flag = 1
                    if not sz_bytes:
                        sz_bytes = [0]
                        sz_flag = 1
                    opcode = 0x80 | off_flag | (sz_flag << 4)
                    delta.append(opcode)
                    delta.extend(off_bytes)
                    delta.extend(sz_bytes)
                    pos += match_len
                    matched = True
        if not matched:
            pending_literal.append(target[pos])
            pos += 1

    flush_literal()
    return bytes(delta)


def apply_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0

    def read_vi() -> int:
        nonlocal pos
        res = 0
        shift = 0
        while True:
            b = delta[pos]
            pos += 1
            res |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        return res

    base_size = read_vi()
    target_size = read_vi()
    if base_size != len(base):
        raise ValueError(f"Delta base size mismatch: {base_size} != {len(base)}")

    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            off = 0
            if op & 0x01:
                off |= delta[pos]
                pos += 1
            if op & 0x02:
                off |= delta[pos] << 8
                pos += 1
            if op & 0x04:
                off |= delta[pos] << 16
                pos += 1
            if op & 0x08:
                off |= delta[pos] << 24
                pos += 1
            sz = 0
            if op & 0x10:
                sz |= delta[pos]
                pos += 1
            if op & 0x20:
                sz |= delta[pos] << 8
                pos += 1
            if op & 0x40:
                sz |= delta[pos] << 16
                pos += 1
            if sz == 0:
                sz = 0x10000
            out.extend(base[off : off + sz])
        elif op > 0:
            out.extend(delta[pos : pos + op])
            pos += op
        else:
            raise ValueError("Invalid delta opcode 0")
    if len(out) != target_size:
        raise ValueError(f"Reconstructed size mismatch {len(out)} != {target_size}")
    return bytes(out)


def encode_ofs_delta_offset(distance: int) -> bytes:
    buf = []
    c = distance & 0x7F
    buf.append(c)
    distance >>= 7
    while distance > 0:
        distance -= 1
        c = (distance & 0x7F) | 0x80
        buf.append(c)
        distance >>= 7
    buf.reverse()
    return bytes(buf)


def decode_ofs_delta_offset(data: bytes, pos: int) -> Tuple[int, int]:
    c = data[pos]
    pos += 1
    offset = c & 0x7F
    while c & 0x80:
        c = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (c & 0x7F)
    return offset, pos


def create_packfile(
    repo_dir: str,
    entries: List[Tuple[str, int, bytes, Optional[Tuple[str, Any]]]],
) -> Tuple[str, str]:
    """Generates .pack and .idx v2 for a given list of objects.

    entries is a list of (oid_hex, type_id, uncompressed_data, delta_info)
    delta_info: None for undeltified, or ("ofs", base_index, delta_bytes)
    """
    pack_buf = bytearray(struct.pack(">4sII", b"PACK", 2, len(entries)))
    offsets: List[int] = []
    crcs: List[int] = []

    # Map entry index to its pack offset
    idx_to_offset: Dict[int, int] = {}

    for i, (oid_hex, type_id, raw_data, delta_info) in enumerate(entries):
        entry_offset = len(pack_buf)
        idx_to_offset[i] = entry_offset
        offsets.append(entry_offset)

        if delta_info is None:
            # Undeltified
            sz = len(raw_data)
            first_byte = ((type_id & 7) << 4) | (sz & 0x0F)
            sz >>= 4
            if sz > 0:
                first_byte |= 0x80
                hdr = bytearray([first_byte])
                while sz > 0:
                    b = sz & 0x7F
                    sz >>= 7
                    if sz > 0:
                        b |= 0x80
                    hdr.append(b)
            else:
                hdr = bytearray([first_byte])
            comp = zlib.compress(raw_data)
            packed_entry = bytes(hdr) + comp
        else:
            kind, base_idx, delta_bytes = delta_info
            assert kind == "ofs"
            base_offset = idx_to_offset[base_idx]
            dist = entry_offset - base_offset
            assert dist > 0

            # Delta header has size of the uncompressed delta
            sz = len(delta_bytes)
            first_byte = ((OBJ_OFS_DELTA & 7) << 4) | (sz & 0x0F)
            sz >>= 4
            if sz > 0:
                first_byte |= 0x80
                hdr = bytearray([first_byte])
                while sz > 0:
                    b = sz & 0x7F
                    sz >>= 7
                    if sz > 0:
                        b |= 0x80
                    hdr.append(b)
            else:
                hdr = bytearray([first_byte])

            ofs_bytes = encode_ofs_delta_offset(dist)
            comp = zlib.compress(delta_bytes)
            packed_entry = bytes(hdr) + ofs_bytes + comp

        crc = zlib.crc32(packed_entry) & 0xFFFFFFFF
        crcs.append(crc)
        pack_buf.extend(packed_entry)

    pack_trailer = hashlib.sha1(pack_buf).digest()
    pack_buf.extend(pack_trailer)

    # Sort objects lexicographically by 20-byte binary OID
    sorted_indices = sorted(range(len(entries)), key=lambda k: bytes.fromhex(entries[k][0]))
    sorted_oids = [bytes.fromhex(entries[k][0]) for k in sorted_indices]
    sorted_crcs = [crcs[k] for k in sorted_indices]
    sorted_offsets = [offsets[k] for k in sorted_indices]

    # Build fanout table
    fanout = [0] * 256
    for oid in sorted_oids:
        fanout[oid[0]] += 1
    for k in range(1, 256):
        fanout[k] += fanout[k - 1]

    idx_buf = bytearray()
    idx_buf.extend(struct.pack(">4sI", b"\xfftOc", 2))
    idx_buf.extend(struct.pack(">256I", *fanout))
    for oid in sorted_oids:
        idx_buf.extend(oid)
    for crc in sorted_crcs:
        idx_buf.extend(struct.pack(">I", crc))
    for off in sorted_offsets:
        idx_buf.extend(struct.pack(">I", off))
    idx_buf.extend(pack_trailer)
    idx_trailer = hashlib.sha1(idx_buf).digest()
    idx_buf.extend(idx_trailer)

    pack_hash = pack_trailer.hex()
    pack_dir = os.path.join(repo_dir, ".git", "objects", "pack")
    os.makedirs(pack_dir, exist_ok=True)
    pack_path = os.path.join(pack_dir, f"pack-{pack_hash}.pack")
    idx_path = os.path.join(pack_dir, f"pack-{pack_hash}.idx")
    with open(pack_path, "wb") as f:
        f.write(pack_buf)
    with open(idx_path, "wb") as f:
        f.write(idx_buf)
    return pack_path, idx_path


def read_packfile_object(pack_path: str, idx_path: str, target_oid_hex: str) -> Tuple[str, bytes]:
    """Locates and reconstructs an object from .pack using .idx v2."""
    with open(idx_path, "rb") as f:
        idx_data = f.read()
    with open(pack_path, "rb") as f:
        pack_data = f.read()

    # Validate trailers
    pack_trailer = pack_data[-20:]
    calc_pack_trailer = hashlib.sha1(pack_data[:-20]).digest()
    if pack_trailer != calc_pack_trailer:
        raise ValueError("Packfile trailer checksum mismatch")
    idx_trailer = idx_data[-20:]
    calc_idx_trailer = hashlib.sha1(idx_data[:-20]).digest()
    if idx_trailer != calc_idx_trailer:
        raise ValueError("Index trailer checksum mismatch")
    if idx_data[-40:-20] != pack_trailer:
        raise ValueError("Index does not reference pack trailer")

    fanout = struct.unpack(">256I", idx_data[8 : 8 + 1024])
    total_objects = fanout[255]
    names_offset = 8 + 1024
    crcs_offset = names_offset + total_objects * 20
    offsets_offset = crcs_offset + total_objects * 4

    target_bytes = bytes.fromhex(target_oid_hex)
    first_byte = target_bytes[0]
    lo = fanout[first_byte - 1] if first_byte > 0 else 0
    hi = fanout[first_byte]

    obj_idx = -1
    while lo < hi:
        mid = (lo + hi) // 2
        cand = idx_data[names_offset + mid * 20 : names_offset + (mid + 1) * 20]
        if cand < target_bytes:
            lo = mid + 1
        elif cand > target_bytes:
            hi = mid
        else:
            obj_idx = mid
            break
    if obj_idx == -1:
        raise KeyError(f"Object {target_oid_hex} not found in pack index")

    pack_offset = struct.unpack(">I", idx_data[offsets_offset + obj_idx * 4 : offsets_offset + (obj_idx + 1) * 4])[0]

    def parse_entry_at(offset: int) -> Tuple[int, bytes]:
        pos = offset
        b = pack_data[pos]
        pos += 1
        type_id = (b >> 4) & 7
        sz = b & 0x0F
        shift = 4
        while b & 0x80:
            b = pack_data[pos]
            pos += 1
            sz |= (b & 0x7F) << shift
            shift += 7

        if type_id == OBJ_OFS_DELTA:
            dist, pos = decode_ofs_delta_offset(pack_data, pos)
            base_offset = offset - dist
            decompressor = zlib.decompressobj()
            delta_bytes = decompressor.decompress(pack_data[pos:])
            base_type_id, base_payload = parse_entry_at(base_offset)
            reconstructed = apply_delta(base_payload, delta_bytes)
            return base_type_id, reconstructed
        elif type_id in TYPE_NAMES:
            decompressor = zlib.decompressobj()
            payload = decompressor.decompress(pack_data[pos:])
            return type_id, payload
        else:
            raise ValueError(f"Unsupported packed object type: {type_id}")

    type_id, payload = parse_entry_at(pack_offset)
    type_name = TYPE_NAMES[type_id]
    # Verify reconstructed SHA-1
    calc_sha = hashlib.sha1(f"{type_name} {len(payload)}\x00".encode("utf-8") + payload).hexdigest()
    if calc_sha != target_oid_hex:
        raise ValueError(f"Reconstructed hash mismatch: {calc_sha} != {target_oid_hex}")
    return type_name, payload


def checkout_tree(repo_dir: str, tree_oid: str, target_dir: str) -> List[IndexEntry]:
    """Recursively checks out a tree to target_dir and creates index entries."""
    os.makedirs(target_dir, exist_ok=True)
    index_entries: List[IndexEntry] = []

    def restore_node(curr_tree_oid: str, prefix: str) -> None:
        obj_type, data = read_loose_object(repo_dir, curr_tree_oid)
        assert obj_type == "tree"
        entries = parse_tree_object(data)
        for mode, item_type, oid, name in entries:
            rel_path = os.path.join(prefix, name) if prefix else name
            if item_type == "tree":
                os.makedirs(os.path.join(target_dir, rel_path), exist_ok=True)
                restore_node(oid, rel_path)
            else:
                _, blob_data = read_loose_object(repo_dir, oid)
                full_path = os.path.join(target_dir, rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "wb") as f:
                    f.write(blob_data)
                # Apply mode permissions
                os.chmod(full_path, mode & 0o777)
                st = os.lstat(full_path)
                entry = IndexEntry(
                    ctime_s=st.st_ctime_ns // 10**9,
                    ctime_ns=st.st_ctime_ns % 10**9,
                    mtime_s=st.st_mtime_ns // 10**9,
                    mtime_ns=st.st_mtime_ns % 10**9,
                    dev=st.st_dev & 0xFFFFFFFF,
                    ino=st.st_ino & 0xFFFFFFFF,
                    mode=mode,
                    uid=st.st_uid,
                    gid=st.st_gid,
                    file_size=len(blob_data),
                    sha1=bytes.fromhex(oid),
                    flags=min(len(rel_path.encode("utf-8")), 0xFFF),
                    path=rel_path,
                )
                index_entries.append(entry)

    restore_node(tree_oid, "")
    index_entries.sort(key=lambda e: e.path)
    return index_entries


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: minigit.py <command> [args...]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "init":
        path = sys.argv[2] if len(sys.argv) > 2 else "."
        init_repo(path)
        print(f"Initialized empty Git repository in {os.path.abspath(path)}/.git/")
    else:
        print(f"Unknown command {cmd}")
        sys.exit(1)
