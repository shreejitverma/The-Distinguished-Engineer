#!/usr/bin/env python3
"""Canonical Git Object Store and Content-Addressing Engine.

Implements the fundamental directed acyclic graph (DAG) storage engine of Git:
- Loose object header serialization: "<type> <size>\\0<payload>"
- zlib deflation and fan-out directory storage: .git/objects/??/????...
- Cryptographic content addressing for SHA-1 (20-byte binary / 40-char hex)
  and SHA-256 (32-byte binary / 64-char hex)
- Binary tree encoding with Git canonical sorting (base_name_compare)
- Commit and annotated tag object serialization and deserialization
- Bit-flip and content-tamper corruption detection
"""

from __future__ import annotations

import binascii
import functools
import hashlib
import os
import secrets
import zlib


# Git's default loose-object zlib level (core.looseCompression falls back to Z_BEST_SPEED).
LOOSE_COMPRESSION_LEVEL = 1


class ObjectCorruptionError(ValueError):
    """Base class for every integrity failure raised by the store."""


class SizeMismatchError(ObjectCorruptionError):
    """The header's declared size differs from the payload length."""


class HashMismatchError(ObjectCorruptionError):
    """The recomputed object ID differs from the ID the object is stored under."""


class TreeOrderError(ObjectCorruptionError):
    """Tree entries are not in Git's canonical order."""


# File modes in octal matching Git tree representation
MODE_DIR = 0o040000        # Directory / Tree
MODE_REGULAR = 0o100644    # Regular non-executable file
MODE_EXECUTABLE = 0o100755 # Executable file
MODE_SYMLINK = 0o120000    # Symbolic link
MODE_GITLINK = 0o160000    # Submodule commit link


def base_name_compare(name1: bytes, mode1: int, name2: bytes, mode2: int) -> int:
    """Compare two tree entry paths using Git canonical sorting.

    In Git tree objects, directories are sorted as if their path name
    ended with an implicit trailing slash ('/'). This ensures correct
    lexicographical ordering between directories and regular files
    sharing common prefixes (e.g., 'bin', 'bin-tool', 'bin.c', 'bina').
    """
    s1 = name1 + (b"/" if (mode1 & 0o170000) == MODE_DIR else b"")
    s2 = name2 + (b"/" if (mode2 & 0o170000) == MODE_DIR else b"")
    return (s1 > s2) - (s1 < s2)


class ObjectStore:
    """Content-addressable object store for Git loose objects."""

    def __init__(self, root_dir: str, object_format: str = "sha1") -> None:
        if object_format not in ("sha1", "sha256"):
            raise ValueError(f"Unsupported object format: {object_format}")
        self.root_dir = os.path.abspath(root_dir)
        self.objects_dir = os.path.join(self.root_dir, ".git", "objects")
        self.object_format = object_format
        self.hash_func = hashlib.sha1 if object_format == "sha1" else hashlib.sha256
        self.hash_bytes_len = 20 if object_format == "sha1" else 32
        self.hash_hex_len = 40 if object_format == "sha1" else 64
        os.makedirs(self.objects_dir, exist_ok=True)

    def _hash(self, data: bytes) -> str:
        return self.hash_func(data).hexdigest()

    def object_path(self, oid: str) -> str:
        """Return the filesystem path for a loose object ID."""
        if len(oid) != self.hash_hex_len:
            raise ValueError(f"Invalid OID length for {self.object_format}: {oid}")
        return os.path.join(self.objects_dir, oid[:2], oid[2:])

    def write_raw_object(self, obj_type: str, payload: bytes) -> str:
        """Serialize, hash, compress, and atomically write a loose object."""
        header = f"{obj_type} {len(payload)}\0".encode("ascii")
        uncompressed = header + payload
        oid = self._hash(uncompressed)
        path = self.object_path(oid)

        if os.path.exists(path):
            return oid

        fanout_dir = os.path.dirname(path)
        os.makedirs(fanout_dir, exist_ok=True)

        compressed = zlib.compress(uncompressed, level=LOOSE_COMPRESSION_LEVEL)

        # Same publish protocol as Git: create a temp file in the fan-out directory
        # with mode 0444 from the start, write it, then link(2) it to its final name
        # and unlink the temp name. link(2) never replaces an existing object.
        tmp_path = os.path.join(fanout_dir, "tmp_obj_" + secrets.token_hex(6))
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(compressed)
            self.publish_noclobber(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

        return oid

    @staticmethod
    def publish_noclobber(tmp_path: str, final_path: str) -> bool:
        """Hard-link tmp_path to final_path and remove tmp_path.

        Returns True if final_path was created, False if it already existed.
        An existing object is kept untouched, as in Git's finalize_object_file.
        """
        try:
            os.link(tmp_path, final_path)
            created = True
        except FileExistsError:
            created = False
        os.unlink(tmp_path)
        return created

    def read_raw_object(self, oid: str) -> tuple[str, bytes]:
        """Read and verify a loose object from disk.

        Returns (obj_type, payload).
        Raises FileNotFoundError if missing.
        Raises zlib.error on compression corruption.
        Raises SizeMismatchError or HashMismatchError (both ValueError) on integrity failure,
        and plain ValueError on a header parse error.
        """
        path = self.object_path(oid)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Object {oid} not found at {path}")

        with open(path, "rb") as f:
            compressed = f.read()

        uncompressed = zlib.decompress(compressed)

        null_idx = uncompressed.find(b"\0")
        if null_idx == -1:
            raise ValueError(f"Corrupted object header: missing null byte in {oid}")

        header_bytes = uncompressed[:null_idx]
        payload = uncompressed[null_idx + 1:]

        try:
            header_str = header_bytes.decode("ascii")
            obj_type, size_str = header_str.split(" ", 1)
            declared_size = int(size_str)
        except Exception as e:
            raise ValueError(f"Failed to parse object header '{header_bytes!r}': {e}") from e

        if declared_size != len(payload):
            raise SizeMismatchError(
                f"Object size mismatch: header declared {declared_size} bytes, "
                f"payload is {len(payload)} bytes"
            )

        computed_oid = self._hash(uncompressed)
        if computed_oid != oid:
            raise HashMismatchError(
                f"Cryptographic hash mismatch: expected {oid}, computed {computed_oid}"
            )

        return obj_type, payload

    # --- High-Level Type Serializers ---

    def write_blob(self, content: bytes) -> str:
        """Write a blob object containing raw uninterpreted data."""
        return self.write_raw_object("blob", content)

    def write_tree(self, entries: list[tuple[int, str, str]]) -> str:
        """Serialize and write a canonical tree object.

        entries is a list of (mode, name, oid).
        Entries are sorted according to Git's base_name_compare rules.
        """
        raw_entries = []
        for mode, name, oid in entries:
            name_bytes = name.encode("utf-8") if isinstance(name, str) else name
            oid_bytes = binascii.unhexlify(oid)
            if len(oid_bytes) != self.hash_bytes_len:
                raise ValueError(f"Invalid OID length {len(oid_bytes)} for {oid}")
            raw_entries.append((mode, name_bytes, oid_bytes))

        # Sort with base_name_compare
        def entry_cmp(a, b):
            return base_name_compare(a[1], a[0], b[1], b[0])

        raw_entries.sort(key=functools.cmp_to_key(entry_cmp))

        payload = bytearray()
        for mode, name_bytes, oid_bytes in raw_entries:
            mode_ascii = f"{mode:o}".encode("ascii")
            payload.extend(mode_ascii)
            payload.append(0x20) # space
            payload.extend(name_bytes)
            payload.append(0x00) # null
            payload.extend(oid_bytes)

        return self.write_raw_object("tree", bytes(payload))

    def parse_tree(self, payload: bytes) -> list[tuple[int, str, str, str]]:
        """Parse raw tree payload into entries.

        Returns list of (mode, name, oid, target_type).
        Validates that entries are in strict canonical sort order.
        """
        entries = []
        idx = 0
        total = len(payload)
        prev_name_mode = None

        while idx < total:
            space_idx = payload.find(b" ", idx)
            if space_idx == -1:
                raise ValueError("Corrupted tree: missing space after mode")

            mode_str = payload[idx:space_idx].decode("ascii")
            mode = int(mode_str, 8)

            null_idx = payload.find(b"\0", space_idx + 1)
            if null_idx == -1:
                raise ValueError("Corrupted tree: missing null terminator after name")

            name_bytes = payload[space_idx + 1:null_idx]
            name = name_bytes.decode("utf-8")

            oid_start = null_idx + 1
            oid_end = oid_start + self.hash_bytes_len
            if oid_end > total:
                raise ValueError("Corrupted tree: truncated object ID bytes")

            oid_bytes = payload[oid_start:oid_end]
            oid = binascii.hexlify(oid_bytes).decode("ascii")

            # Check sort ordering
            if prev_name_mode is not None:
                p_name, p_mode = prev_name_mode
                cmp_res = base_name_compare(p_name, p_mode, name_bytes, mode)
                if cmp_res >= 0:
                    raise TreeOrderError(
                        f"Tree entries out of canonical order: '{p_name.decode()}' >= '{name}'"
                    )

            prev_name_mode = (name_bytes, mode)

            # Determine target object type from mode
            if (mode & 0o170000) == MODE_DIR:
                target_type = "tree"
            elif mode == MODE_GITLINK:
                target_type = "commit"
            else:
                target_type = "blob"

            entries.append((mode, name, oid, target_type))
            idx = oid_end

        return entries

    def write_commit(
        self,
        tree_oid: str,
        parent_oids: list[str],
        author_name: str,
        author_email: str,
        author_epoch: int,
        author_tz: str,
        committer_name: str,
        committer_email: str,
        committer_epoch: int,
        committer_tz: str,
        message: str,
    ) -> str:
        """Serialize and write a commit object."""
        lines = [f"tree {tree_oid}"]
        for parent in parent_oids:
            lines.append(f"parent {parent}")
        lines.append(f"author {author_name} <{author_email}> {author_epoch} {author_tz}")
        lines.append(f"committer {committer_name} <{committer_email}> {committer_epoch} {committer_tz}")
        lines.append("")
        lines.append(message.rstrip("\n") + "\n")

        payload = "\n".join(lines).encode("utf-8")
        return self.write_raw_object("commit", payload)

    def parse_commit(self, payload: bytes) -> dict:
        """Parse raw commit payload into header fields and message body."""
        text = payload.decode("utf-8", errors="replace")
        header_part, _, message = text.partition("\n\n")

        lines = header_part.split("\n")
        tree = None
        parents = []
        author = None
        committer = None

        for line in lines:
            if line.startswith("tree "):
                tree = line[5:].strip()
            elif line.startswith("parent "):
                parents.append(line[7:].strip())
            elif line.startswith("author "):
                author = line[7:].strip()
            elif line.startswith("committer "):
                committer = line[10:].strip()

        return {
            "tree": tree,
            "parents": parents,
            "author": author,
            "committer": committer,
            "message": message,
        }

    def write_tag(
        self,
        target_oid: str,
        target_type: str,
        tag_name: str,
        tagger_name: str,
        tagger_email: str,
        tagger_epoch: int,
        tagger_tz: str,
        message: str,
    ) -> str:
        """Serialize and write an annotated tag object."""
        lines = [
            f"object {target_oid}",
            f"type {target_type}",
            f"tag {tag_name}",
            f"tagger {tagger_name} <{tagger_email}> {tagger_epoch} {tagger_tz}",
            "",
            message.rstrip("\n") + "\n",
        ]
        payload = "\n".join(lines).encode("utf-8")
        return self.write_raw_object("tag", payload)

    def parse_tag(self, payload: bytes) -> dict:
        """Parse raw tag payload into header fields and message body."""
        text = payload.decode("utf-8", errors="replace")
        header_part, _, message = text.partition("\n\n")

        lines = header_part.split("\n")
        fields = {}
        for line in lines:
            if " " in line:
                k, v = line.split(" ", 1)
                fields[k] = v.strip()

        return {
            "object": fields.get("object"),
            "type": fields.get("type"),
            "tag": fields.get("tag"),
            "tagger": fields.get("tagger"),
            "message": message,
        }
