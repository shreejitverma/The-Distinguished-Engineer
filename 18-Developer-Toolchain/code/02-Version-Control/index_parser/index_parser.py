"""Git index file parser and serializer across formats v2, v3, and v4.

Implements the on-disk index layout specified in gitformat-index(5),
including stat cache evaluation (ce_match_stat_basic), racy-Git detection,
conflict stage handling, and index extensions (TREE, REUC, EOIE).
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Dict, List, Optional, Tuple

DIRC_MAGIC = b"DIRC"

# Flag bitmasks in 16-bit flags field
CE_VALID = 0x8000        # Assume-valid / assume-unchanged
CE_EXTENDED = 0x4000     # Extended flags follow (v3+)
CE_STAGEMASK = 0x3000    # Stage mask
CE_STAGESHIFT = 12       # Shift for stage bits
CE_NAMEMASK = 0x0FFF     # Name length mask (capped at 4095)

# Extended flag bitmasks in 16-bit flags2 field (v3+)
CE_EXT_RESERVED = 0x8000     # Reserved for future
CE_SKIP_WORKTREE = 0x4000    # Skip-worktree bit (sparse checkout)
CE_INTENT_TO_ADD = 0x2000    # Intent-to-add bit (git add -N)

# Common file modes
MODE_REGULAR = 0o100644
MODE_EXECUTABLE = 0o100755
MODE_SYMLINK = 0o120000
MODE_GITLINK = 0o160000
MODE_DIR = 0o040000


def encode_varint(value: int) -> bytes:
    """Encode an integer using Git's variable-width offset encoding."""
    buf = bytearray()
    buf.append(value & 0x7F)
    value >>= 7
    while value > 0:
        value -= 1
        buf.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(buf))


def decode_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """Decode a variable-width integer from data starting at offset."""
    pos = offset
    b = data[pos]
    pos += 1
    val = b & 0x7F
    while b & 0x80:
        b = data[pos]
        pos += 1
        val = ((val + 1) << 7) | (b & 0x7F)
    return val, pos


@dataclass
class StatData:
    ctime_sec: int = 0
    ctime_nsec: int = 0
    mtime_sec: int = 0
    mtime_nsec: int = 0
    dev: int = 0
    ino: int = 0
    mode: int = MODE_REGULAR
    uid: int = 0
    gid: int = 0
    file_size: int = 0

    @classmethod
    def from_os_stat(cls, st: os.stat_result) -> StatData:
        ctime_ns = getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9))
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        return cls(
            ctime_sec=int(st.st_ctime),
            ctime_nsec=ctime_ns % 1_000_000_000,
            mtime_sec=int(st.st_mtime),
            mtime_nsec=mtime_ns % 1_000_000_000,
            dev=st.st_dev & 0xFFFFFFFF,
            ino=st.st_ino & 0xFFFFFFFF,
            mode=st.st_mode,
            uid=st.st_uid,
            gid=st.st_gid,
            file_size=munge_st_size(st.st_size),
        )


@dataclass
class IndexEntry:
    path: str
    oid: bytes  # 20 bytes for SHA-1, 32 bytes for SHA-256
    stat: StatData = field(default_factory=StatData)
    stage: int = 0
    assume_valid: bool = False
    skip_worktree: bool = False
    intent_to_add: bool = False

    @property
    def oid_hex(self) -> str:
        return self.oid.hex()


@dataclass
class CacheTreeNode:
    path: str
    entry_count: int  # -1 if invalidated
    subtree_count: int
    oid: Optional[bytes] = None  # None if entry_count == -1


@dataclass
class ResolveUndoEntry:
    path: str
    stages: Dict[int, Tuple[int, bytes]] = field(default_factory=dict)
    # mapping of stage (1, 2, 3) -> (mode, oid)


class GitIndex:
    def __init__(self, version: int = 2, oid_len: int = 20):
        self.version = version
        self.oid_len = oid_len
        self.entries: List[IndexEntry] = []
        self.cache_tree: List[CacheTreeNode] = []
        self.resolve_undo: List[ResolveUndoEntry] = []
        self.eoie_offset: Optional[int] = None
        # Raw (signature, body) extensions written after the known ones.
        self.extra_extensions: List[Tuple[bytes, bytes]] = []
        self.trailing_checksum: Optional[bytes] = None

    def add_entry(self, entry: IndexEntry) -> None:
        self.entries.append(entry)
        # Keep entries strictly sorted by (path, stage)
        self.entries.sort(key=lambda e: (e.path.encode("utf-8"), e.stage))

    def serialize(self) -> bytes:
        """Serialize index into binary format."""
        out = bytearray()
        # 12-byte header
        out.extend(struct.pack(">4sII", DIRC_MAGIC, self.version, len(self.entries)))

        prev_path = ""
        for entry in self.entries:
            st = entry.stat
            # 40 bytes stat cache
            out.extend(
                struct.pack(
                    ">10I",
                    st.ctime_sec,
                    st.ctime_nsec,
                    st.mtime_sec,
                    st.mtime_nsec,
                    st.dev,
                    st.ino,
                    st.mode,
                    st.uid,
                    st.gid,
                    st.file_size,
                )
            )
            # OID
            out.extend(entry.oid)

            # Flags
            name_bytes = entry.path.encode("utf-8")
            namelen = min(len(name_bytes), CE_NAMEMASK)
            flags = (namelen & CE_NAMEMASK) | ((entry.stage & 0x3) << CE_STAGESHIFT)
            if entry.assume_valid:
                flags |= CE_VALID

            has_ext = entry.skip_worktree or entry.intent_to_add
            if has_ext and self.version >= 3:
                flags |= CE_EXTENDED

            out.extend(struct.pack(">H", flags))

            if flags & CE_EXTENDED:
                flags2 = 0
                if entry.skip_worktree:
                    flags2 |= CE_SKIP_WORKTREE
                if entry.intent_to_add:
                    flags2 |= CE_INTENT_TO_ADD
                out.extend(struct.pack(">H", flags2))

            # Path encoding
            if self.version == 4:
                # Prefix compression
                # Count common prefix bytes with prev_path
                prev_bytes = prev_path.encode("utf-8")
                common = 0
                max_common = min(len(prev_bytes), len(name_bytes))
                while common < max_common and prev_bytes[common] == name_bytes[common]:
                    common += 1
                strip_count = len(prev_bytes) - common
                varint_bytes = encode_varint(strip_count)
                suffix = name_bytes[common:] + b"\x00"
                out.extend(varint_bytes)
                out.extend(suffix)
                prev_path = entry.path
            else:
                # v2 and v3: full path + NUL + padding to 8-byte boundary
                out.extend(name_bytes)
                out.append(0)
                # Calculate entry length so far
                entry_meta_len = 40 + self.oid_len + 2
                if flags & CE_EXTENDED:
                    entry_meta_len += 2
                entry_len = entry_meta_len + len(name_bytes) + 1
                # Git pads with 1 to 8 NUL bytes in total, and the NUL that
                # terminates the name is the first of them, so an entry whose
                # length is already a multiple of 8 gets no extra padding.
                pad = (8 - (entry_len % 8)) % 8
                out.extend(b"\x00" * pad)
                prev_path = entry.path

        # End of index entry (EOIE) pointer position
        eoie_start_offset = len(out)

        # Extensions
        if self.cache_tree:
            ext_data = bytearray()
            for node in self.cache_tree:
                ext_data.extend(node.path.encode("utf-8"))
                ext_data.append(0)
                line = f"{node.entry_count} {node.subtree_count}\n".encode("ascii")
                ext_data.extend(line)
                if node.entry_count >= 0 and node.oid:
                    ext_data.extend(node.oid)
            out.extend(struct.pack(">4sI", b"TREE", len(ext_data)))
            out.extend(ext_data)

        if self.resolve_undo:
            ext_data = bytearray()
            for ru in self.resolve_undo:
                ext_data.extend(ru.path.encode("utf-8"))
                ext_data.append(0)
                # 3 octal mode strings
                for s in (1, 2, 3):
                    if s in ru.stages:
                        ext_data.extend(f"{ru.stages[s][0]:o}\x00".encode("ascii"))
                    else:
                        ext_data.extend(b"0\x00")
                # OIDs
                for s in (1, 2, 3):
                    if s in ru.stages:
                        ext_data.extend(ru.stages[s][1])
            out.extend(struct.pack(">4sI", b"REUC", len(ext_data)))
            out.extend(ext_data)

        for sig, body in self.extra_extensions:
            out.extend(struct.pack(">4sI", sig, len(body)))
            out.extend(body)

        # Trailing checksum
        checksum = hashlib.sha1(out).digest()
        out.extend(checksum)
        return bytes(out)

    @classmethod
    def parse(cls, data: bytes, oid_len: int = 20) -> GitIndex:
        """Parse binary index data into a GitIndex instance."""
        if len(data) < 12 + oid_len:
            raise ValueError(f"Index buffer too small ({len(data)} bytes)")

        # Verify trailer checksum first
        content = data[:-oid_len]
        trailer = data[-oid_len:]
        if oid_len == 20:
            expected_hash = hashlib.sha1(content).digest()
        elif oid_len == 32:
            expected_hash = hashlib.sha256(content).digest()
        else:
            raise ValueError(f"Unsupported OID length: {oid_len}")

        if trailer != expected_hash:
            raise ValueError(
                f"Checksum mismatch: expected {expected_hash.hex()}, got {trailer.hex()}"
            )

        magic, version, num_entries = struct.unpack_from(">4sII", data, 0)
        if magic != DIRC_MAGIC:
            raise ValueError(f"Invalid index magic: {magic!r}")
        if version not in (2, 3, 4):
            raise ValueError(f"Unsupported index version: {version}")

        idx = cls(version=version, oid_len=oid_len)
        idx.trailing_checksum = trailer
        offset = 12
        prev_path = ""

        # Parse entries
        for _ in range(num_entries):
            entry_start = offset
            st_fields = struct.unpack_from(">10I", data, offset)
            offset += 40
            stat_obj = StatData(*st_fields)

            oid = data[offset : offset + oid_len]
            offset += oid_len

            (flags,) = struct.unpack_from(">H", data, offset)
            offset += 2

            assume_valid = bool(flags & CE_VALID)
            stage = (flags & CE_STAGEMASK) >> CE_STAGESHIFT

            skip_worktree = False
            intent_to_add = False
            if flags & CE_EXTENDED:
                if version < 3:
                    raise ValueError("CE_EXTENDED flag set in version 2 index")
                (flags2,) = struct.unpack_from(">H", data, offset)
                offset += 2
                skip_worktree = bool(flags2 & CE_SKIP_WORKTREE)
                intent_to_add = bool(flags2 & CE_INTENT_TO_ADD)

            if version == 4:
                strip_count, offset = decode_varint(data, offset)
                nul_pos = data.find(b"\x00", offset)
                if nul_pos == -1:
                    raise ValueError("Unterminated path in v4 entry")
                suffix = data[offset:nul_pos].decode("utf-8")
                offset = nul_pos + 1
                if strip_count > len(prev_path):
                    raise ValueError("Malformed v4 prefix strip count")
                prefix = prev_path[: len(prev_path) - strip_count]
                path = prefix + suffix
                prev_path = path
            else:
                nul_pos = data.find(b"\x00", offset)
                if nul_pos == -1:
                    raise ValueError("Unterminated path name in index entry")
                path = data[offset:nul_pos].decode("utf-8")
                offset = nul_pos + 1
                entry_len = offset - entry_start
                # Advance past padding
                pad = (8 - (entry_len % 8)) % 8
                offset += pad
                prev_path = path

            entry = IndexEntry(
                path=path,
                oid=oid,
                stat=stat_obj,
                stage=stage,
                assume_valid=assume_valid,
                skip_worktree=skip_worktree,
                intent_to_add=intent_to_add,
            )
            idx.entries.append(entry)

        # Check entry ordering
        for i in range(len(idx.entries) - 1):
            e1 = idx.entries[i]
            e2 = idx.entries[i + 1]
            key1 = (e1.path.encode("utf-8"), e1.stage)
            key2 = (e2.path.encode("utf-8"), e2.stage)
            if key1 >= key2:
                raise ValueError(
                    f"Index entries out of order: {e1.path} (stage {e1.stage}) >= {e2.path} (stage {e2.stage})"
                )

        # Parse extensions
        while offset < len(data) - oid_len:
            ext_sig, ext_size = struct.unpack_from(">4sI", data, offset)
            offset += 8
            ext_body = data[offset : offset + ext_size]
            offset += ext_size

            if ext_sig == b"TREE":
                idx.cache_tree = cls._parse_cache_tree(ext_body, oid_len)
            elif ext_sig == b"REUC":
                idx.resolve_undo = cls._parse_resolve_undo(ext_body, oid_len)
            elif ext_sig == b"EOIE":
                idx.eoie_offset = struct.unpack_from(">I", ext_body, 0)[0]
            else:
                # If optional (starts with 'A'..'Z'), skip
                # If mandatory (starts with 'a'..'z'), reject if unknown
                first_byte = ext_sig[0]
                if ord("a") <= first_byte <= ord("z"):
                    raise ValueError(
                        f"Unknown mandatory index extension: {ext_sig.decode('ascii', errors='replace')}"
                    )

        return idx

    @staticmethod
    def _parse_cache_tree(data: bytes, oid_len: int) -> List[CacheTreeNode]:
        nodes = []
        offset = 0
        while offset < len(data):
            nul = data.find(b"\x00", offset)
            if nul == -1:
                break
            path = data[offset:nul].decode("utf-8")
            offset = nul + 1
            nl = data.find(b"\n", offset)
            if nl == -1:
                break
            line = data[offset:nl].decode("ascii")
            offset = nl + 1
            parts = line.split(" ")
            entry_count = int(parts[0])
            subtree_count = int(parts[1])
            oid = None
            if entry_count >= 0:
                oid = data[offset : offset + oid_len]
                offset += oid_len
            nodes.append(CacheTreeNode(path, entry_count, subtree_count, oid))
        return nodes

    @staticmethod
    def _parse_resolve_undo(data: bytes, oid_len: int) -> List[ResolveUndoEntry]:
        entries = []
        offset = 0
        while offset < len(data):
            nul = data.find(b"\x00", offset)
            if nul == -1:
                break
            path = data[offset:nul].decode("utf-8")
            offset = nul + 1
            # 3 stages octal modes
            modes = {}
            for s in (1, 2, 3):
                m_nul = data.find(b"\x00", offset)
                mode_str = data[offset:m_nul].decode("ascii")
                offset = m_nul + 1
                mode_val = int(mode_str, 8) if mode_str != "0" else 0
                if mode_val != 0:
                    modes[s] = mode_val
            # OIDs for non-zero modes
            stages = {}
            for s in (1, 2, 3):
                if s in modes:
                    oid = data[offset : offset + oid_len]
                    offset += oid_len
                    stages[s] = (modes[s], oid)
            entries.append(ResolveUndoEntry(path, stages))
        return entries


# Change bits, exactly as defined in Git's statinfo.h (v2.55.0)
MTIME_CHANGED = 0x0001
CTIME_CHANGED = 0x0002
OWNER_CHANGED = 0x0004
MODE_CHANGED = 0x0008
INODE_CHANGED = 0x0010
DATA_CHANGED = 0x0020
TYPE_CHANGED = 0x0040

CHANGE_NAMES = (
    (MTIME_CHANGED, "MTIME"),
    (CTIME_CHANGED, "CTIME"),
    (OWNER_CHANGED, "OWNER"),
    (MODE_CHANGED, "MODE"),
    (INODE_CHANGED, "INODE"),
    (DATA_CHANGED, "DATA"),
    (TYPE_CHANGED, "TYPE"),
)

EMPTY_BLOB_OID = bytes.fromhex("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")


def describe_changes(mask: int) -> str:
    """Render a change mask as `0xNNNN NAME|NAME`, or `0x0000 clean`."""
    names = "|".join(n for bit, n in CHANGE_NAMES if mask & bit)
    return f"{mask:#06x} {names or 'clean'}"


def blob_oid(content: bytes) -> bytes:
    """SHA-1 object id of a blob with the given content."""
    header = b"blob %d\x00" % len(content)
    return hashlib.sha1(header + content).digest()


def munge_st_size(size: int) -> int:
    """Mirror statinfo.c munge_st_size(): keep a 4 GiB multiple from reading as 0."""
    low = size & 0xFFFFFFFF
    if low == 0 and size:
        return 0x80000000
    return low


def match_stat_data(
    sd: StatData,
    st: os.stat_result,
    *,
    trust_ctime: bool = True,
    check_stat: bool = True,
    use_stdev: bool = False,
) -> int:
    """Mirror statinfo.c match_stat_data() for a build without USE_NSEC.

    `check_stat` is core.checkStat=default; `use_stdev` models the USE_STDEV
    build option, which is off in a default Git build.
    """
    changed = 0
    if sd.mtime_sec != (int(st.st_mtime) & 0xFFFFFFFF):
        changed |= MTIME_CHANGED
    if trust_ctime and check_stat and sd.ctime_sec != (int(st.st_ctime) & 0xFFFFFFFF):
        changed |= CTIME_CHANGED
    if check_stat:
        if sd.uid != (st.st_uid & 0xFFFFFFFF) or sd.gid != (st.st_gid & 0xFFFFFFFF):
            changed |= OWNER_CHANGED
        if sd.ino != (st.st_ino & 0xFFFFFFFF):
            changed |= INODE_CHANGED
    if use_stdev and check_stat and sd.dev != (st.st_dev & 0xFFFFFFFF):
        changed |= INODE_CHANGED
    if sd.file_size != munge_st_size(st.st_size):
        changed |= DATA_CHANGED
    return changed


def ce_match_stat_basic(
    entry: IndexEntry,
    st: os.stat_result,
    *,
    trust_ctime: bool = True,
    check_stat: bool = True,
    use_stdev: bool = False,
    trust_executable_bit: bool = True,
) -> int:
    """Mirror read-cache.c ce_match_stat_basic(): stat-only comparison.

    Returns a bitmask of the *_CHANGED constants, 0 if the stat data matches.
    It does not look at CE_VALID; that bypass lives in ie_match_stat().
    """
    changed = 0
    ce_mode = entry.stat.mode
    fmt = stat.S_IFMT(ce_mode)

    if fmt == stat.S_IFREG:
        if not stat.S_ISREG(st.st_mode):
            changed |= TYPE_CHANGED
        # Only the owner x bit counts as a mode change.
        if trust_executable_bit and (0o100 & (ce_mode ^ st.st_mode)):
            changed |= MODE_CHANGED
    elif fmt == stat.S_IFLNK:
        if not stat.S_ISLNK(st.st_mode):
            changed |= TYPE_CHANGED
    else:
        raise ValueError(f"unsupported ce_mode: {ce_mode:o}")

    changed |= match_stat_data(
        entry.stat, st, trust_ctime=trust_ctime, check_stat=check_stat, use_stdev=use_stdev
    )

    # Racily smudged entry: a cached size of 0 on a non-empty blob never matches.
    if entry.stat.file_size == 0 and entry.oid != EMPTY_BLOB_OID:
        changed |= DATA_CHANGED
    return changed


def ce_modified_check_fs(entry: IndexEntry, path: str) -> int:
    """Mirror ce_modified_check_fs(): hash the file and compare to the entry's oid."""
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError:
        return DATA_CHANGED
    return 0 if blob_oid(content) == entry.oid else DATA_CHANGED


def is_racy_timestamp(entry: IndexEntry, index_mtime_sec: int) -> bool:
    """Mirror is_racy_stat() for a build without USE_NSEC.

    An entry is racy when the index file's mtime second is not newer than
    the entry's cached mtime second.
    """
    return bool(index_mtime_sec) and index_mtime_sec <= entry.stat.mtime_sec


def ie_match_stat(
    entry: IndexEntry,
    st: os.stat_result,
    path: str,
    index_mtime_sec: int,
    **stat_opts: bool,
) -> int:
    """Mirror read-cache.c ie_match_stat(): valid bits, stat check, racy content check."""
    if entry.skip_worktree:
        return 0
    if entry.assume_valid:
        return 0
    if entry.intent_to_add:
        return DATA_CHANGED | TYPE_CHANGED | MODE_CHANGED

    changed = ce_match_stat_basic(entry, st, **stat_opts)
    if not changed and is_racy_timestamp(entry, index_mtime_sec):
        changed |= ce_modified_check_fs(entry, path)
    return changed


def smudge_racily_clean_entry(
    entry: IndexEntry, path: str, index_mtime_sec: int, **stat_opts: bool
) -> bool:
    """Mirror ce_smudge_racily_clean_entry(): zero the cached size of a falsely clean entry.

    Called for racy entries only. The entry is smudged only when the stat data
    still matches but the file content no longer matches the staged oid.
    Returns True if the entry was smudged.
    """
    if not is_racy_timestamp(entry, index_mtime_sec):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if ce_match_stat_basic(entry, st, **stat_opts):
        return False
    if ce_modified_check_fs(entry, path):
        entry.stat.file_size = 0
        return True
    return False
