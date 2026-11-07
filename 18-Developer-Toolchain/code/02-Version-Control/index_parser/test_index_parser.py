#!/usr/bin/env python3
"""Deterministic verification test suite for Git Index Parser Lab."""

import copy
import hashlib
import os
import shutil
import struct
import tempfile

from git_fixtures import REAL_GIT_INDEX_REUC_HEX, REAL_GIT_INDEX_V2_HEX, REAL_GIT_INDEX_V4_HEX
from index_parser import (
    CTIME_CHANGED,
    DATA_CHANGED,
    DIRC_MAGIC,
    EMPTY_BLOB_OID,
    INODE_CHANGED,
    MODE_CHANGED,
    MODE_EXECUTABLE,
    MODE_REGULAR,
    MTIME_CHANGED,
    OWNER_CHANGED,
    TYPE_CHANGED,
    CacheTreeNode,
    GitIndex,
    IndexEntry,
    ResolveUndoEntry,
    StatData,
    blob_oid,
    ce_match_stat_basic,
    decode_varint,
    describe_changes,
    encode_varint,
    ie_match_stat,
    is_racy_timestamp,
    munge_st_size,
    smudge_racily_clean_entry,
)


def write_file(path, content, mtime):
    """Create a file with fixed content and a fixed (integer second) mtime."""
    with open(path, "wb") as fh:
        fh.write(content)
    os.utime(path, (mtime, mtime))


def entry_for(path, content, name="f"):
    """Index entry whose cached stat data and oid describe the file as it is now."""
    return IndexEntry(path=name, oid=blob_oid(content), stat=StatData.from_os_stat(os.lstat(path)))


def raw_v2_flag_words(raw):
    """Walk a v2/v3 index with Git's own size rule and return (flags, flags2) per entry.

    Independent of GitIndex.parse(): it reads the on-disk bytes directly, so a
    wrong bit constant in the lab cannot cancel out between writer and reader.
    """
    count = struct.unpack_from(">I", raw, 8)[0]
    off = 12
    words = []
    for _ in range(count):
        (flags,) = struct.unpack_from(">H", raw, off + 60)
        name_off = off + 62
        flags2 = None
        if flags & 0x4000:
            (flags2,) = struct.unpack_from(">H", raw, off + 62)
            name_off += 2
        name_len = raw.index(b"\x00", name_off) - name_off
        words.append((flags, flags2))
        off += ((name_off - off) + name_len + 8) & ~7
    return words


def run_lab():
    print("=== Step 1: Git Index Version 2 Serialization and Parsing ===")
    idx_v2 = GitIndex(version=2)

    # Deterministic dummy OIDs
    oid_blob1 = hashlib.sha1(b"blob 11\x00hello world\n").digest()
    oid_blob2 = hashlib.sha1(b"blob 15\x00second content\n").digest()

    st1 = StatData(
        ctime_sec=1700000000,
        ctime_nsec=123456789,
        mtime_sec=1700000000,
        mtime_nsec=123456789,
        dev=16777220,
        ino=8675309,
        mode=MODE_REGULAR,
        uid=1000,
        gid=1000,
        file_size=12,
    )
    e1 = IndexEntry(path="docs/readme.txt", oid=oid_blob1, stat=st1, stage=0)

    st2 = StatData(
        ctime_sec=1700000010,
        ctime_nsec=987654321,
        mtime_sec=1700000010,
        mtime_nsec=987654321,
        dev=16777220,
        ino=8675310,
        mode=MODE_EXECUTABLE,
        uid=1000,
        gid=1000,
        file_size=15,
    )
    e2 = IndexEntry(path="src/main.sh", oid=oid_blob2, stat=st2, stage=0)

    idx_v2.add_entry(e1)
    idx_v2.add_entry(e2)

    raw_v2 = idx_v2.serialize()
    print(f"Serialized v2 index size: {len(raw_v2)} bytes")
    # Verify header
    magic, ver, count = raw_v2[:4], int.from_bytes(raw_v2[4:8], "big"), int.from_bytes(raw_v2[8:12], "big")
    print(f"Header: magic={magic.decode('ascii')} version={ver} entries={count}")
    assert magic == DIRC_MAGIC
    assert ver == 2
    assert count == 2

    # Parse back
    parsed_v2 = GitIndex.parse(raw_v2)
    assert len(parsed_v2.entries) == 2
    print(f"Parsed entry 0: path='{parsed_v2.entries[0].path}' mode={oct(parsed_v2.entries[0].stat.mode)} stage={parsed_v2.entries[0].stage}")
    print(f"Parsed entry 1: path='{parsed_v2.entries[1].path}' mode={oct(parsed_v2.entries[1].stat.mode)} stage={parsed_v2.entries[1].stage}")
    print(f"Trailer checksum verified: {parsed_v2.trailing_checksum.hex()[:16]}... OK")

    print("\n=== Step 2: Git Index Version 3 with Extended Flags ===")
    idx_v3 = GitIndex(version=3)
    oid_sparse = hashlib.sha1(b"sparse blob").digest()
    oid_intent = hashlib.sha1(b"intent blob").digest()

    e_sparse = IndexEntry(
        path="huge_folder/sparse_file.dat",
        oid=oid_sparse,
        stat=StatData(mode=MODE_REGULAR, file_size=1024),
        skip_worktree=True,
    )
    e_intent = IndexEntry(
        path="new_feature/untracked_template.txt",
        oid=oid_intent,
        stat=StatData(mode=MODE_REGULAR, file_size=0),
        intent_to_add=True,
    )
    idx_v3.add_entry(e_sparse)
    idx_v3.add_entry(e_intent)

    raw_v3 = idx_v3.serialize()
    parsed_v3 = GitIndex.parse(raw_v3)
    print(f"Index v3 parsed entries: {len(parsed_v3.entries)}")
    for entry in parsed_v3.entries:
        print(f"  {entry.path}: skip_worktree={entry.skip_worktree} intent_to_add={entry.intent_to_add}")
    assert parsed_v3.entries[0].skip_worktree is True
    assert parsed_v3.entries[1].intent_to_add is True
    words = raw_v2_flag_words(raw_v3)
    print(f"Raw extended flag words: {[f'{w[1]:#06x}' for w in words]}")
    assert words[0][1] == 0x4000 and words[1][1] == 0x2000
    assert all(w[0] & 0x4000 for w in words)

    idx_valid = GitIndex(version=2)
    idx_valid.add_entry(IndexEntry(path="valid.c", oid=hashlib.sha1(b"v").digest(), assume_valid=True))
    raw_valid = idx_valid.serialize()
    flags_valid = raw_v2_flag_words(raw_valid)[0][0]
    print(f"Assume-valid entry raw flags: {flags_valid:#06x} (CE_VALID 0x8000 | name length 7)")
    assert flags_valid == 0x8007
    assert GitIndex.parse(raw_valid).entries[0].assume_valid is True

    print("\n=== Step 3: Git Index Version 4 Prefix Compression ===")
    # Create realistic paths with common directory hierarchy
    paths = [
        "kernel/locking/mutex.c",
        "kernel/locking/rwsem.c",
        "kernel/locking/spinlock.c",
        "kernel/sched/core.c",
        "kernel/sched/fair.c",
        "kernel/sched/idle.c",
    ]
    idx_v2_comp = GitIndex(version=2)
    idx_v4_comp = GitIndex(version=4)

    for p in paths:
        dummy_oid = hashlib.sha1(p.encode("utf-8")).digest()
        idx_v2_comp.add_entry(IndexEntry(path=p, oid=dummy_oid, stat=StatData(file_size=5000)))
        idx_v4_comp.add_entry(IndexEntry(path=p, oid=dummy_oid, stat=StatData(file_size=5000)))

    raw_v2_comp = idx_v2_comp.serialize()
    raw_v4_comp = idx_v4_comp.serialize()
    parsed_v4 = GitIndex.parse(raw_v4_comp)

    print(f"v2 index size (full paths + 8-byte padding): {len(raw_v2_comp)} bytes")
    print(f"v4 index size (varint prefix compression):   {len(raw_v4_comp)} bytes")
    savings = (1.0 - len(raw_v4_comp) / len(raw_v2_comp)) * 100
    print(f"Prefix compression space savings: {savings:.1f}%")

    assert [e.path for e in parsed_v4.entries] == paths
    print(f"Decoded {len(parsed_v4.entries)} paths identically to input paths: OK")

    # Verify varint encoding boundaries
    test_ints = [0, 1, 127, 128, 255, 300, 16384]
    for val in test_ints:
        encoded = encode_varint(val)
        decoded, _ = decode_varint(encoded)
        assert decoded == val
    print(f"Varint encoding/decoding verified across {len(test_ints)} boundaries: OK")

    print("\n=== Step 4: Three-Way Merge Conflict Stages (Stages 1, 2, 3) ===")
    idx_conflict = GitIndex(version=2)
    oid_ancestor = hashlib.sha1(b"ancestor base version\n").digest()
    oid_ours = hashlib.sha1(b"ours branch version\n").digest()
    oid_theirs = hashlib.sha1(b"theirs branch version\n").digest()

    e_clean = IndexEntry(path="alpha.c", oid=hashlib.sha1(b"clean").digest(), stage=0)
    e_stage1 = IndexEntry(path="conflict.c", oid=oid_ancestor, stage=1)
    e_stage2 = IndexEntry(path="conflict.c", oid=oid_ours, stage=2)
    e_stage3 = IndexEntry(path="conflict.c", oid=oid_theirs, stage=3)
    e_omega = IndexEntry(path="omega.c", oid=hashlib.sha1(b"omega").digest(), stage=0)

    # Add out of order to verify sorting logic
    idx_conflict.add_entry(e_stage3)
    idx_conflict.add_entry(e_clean)
    idx_conflict.add_entry(e_stage1)
    idx_conflict.add_entry(e_omega)
    idx_conflict.add_entry(e_stage2)

    raw_conflict = idx_conflict.serialize()
    parsed_conflict = GitIndex.parse(raw_conflict)

    print(f"Conflict index total entries: {len(parsed_conflict.entries)}")
    for e in parsed_conflict.entries:
        print(f"  path='{e.path}' stage={e.stage} oid_prefix={e.oid_hex[:8]}")

    # Verify strict stage ordering: alpha.c(0) -> conflict.c(1) -> conflict.c(2) -> conflict.c(3) -> omega.c(0)
    stages = [(e.path, e.stage) for e in parsed_conflict.entries]
    expected_stages = [
        ("alpha.c", 0),
        ("conflict.c", 1),
        ("conflict.c", 2),
        ("conflict.c", 3),
        ("omega.c", 0),
    ]
    assert stages == expected_stages
    print("Conflict stages sorting verified: OK")
    raw_stage_bits = [(w[0] >> 12) & 0x3 for w in raw_v2_flag_words(raw_conflict)]
    print(f"Raw stage bits per entry: {raw_stage_bits}")
    assert raw_stage_bits == [0, 1, 2, 3, 0]

    print("\n=== Step 5: Stat Cache Matching Engine (ce_match_stat_basic) ===")
    # Every file mtime is forced to a fixed second and ctime is excluded with
    # trust_ctime=False wherever a real write would move it, so each result is
    # an exact bitmask that does not depend on the wall clock.
    no_ctime = {"trust_ctime": False}
    work = tempfile.mkdtemp(prefix="git_index_stat_")
    try:
        t0 = 1700000000
        base = b"tracked content line\n"
        path = os.path.join(work, "tracked.txt")
        write_file(path, base, t0)
        entry = entry_for(path, base)
        st = os.lstat(path)

        res = ce_match_stat_basic(entry, st)
        print(f"Clean stat match: {describe_changes(res)}")
        assert res == 0

        e = copy.deepcopy(entry)
        e.stat.mtime_sec -= 1
        res = ce_match_stat_basic(e, st)
        print(f"Cached mtime off by one second: {describe_changes(res)}")
        assert res == MTIME_CHANGED == 0x0001

        e = copy.deepcopy(entry)
        e.stat.ctime_sec -= 1
        res = ce_match_stat_basic(e, st)
        res_off = ce_match_stat_basic(e, st, trust_ctime=False)
        print(f"Cached ctime off by one second: {describe_changes(res)}; with core.trustctime=false: {describe_changes(res_off)}")
        assert res == CTIME_CHANGED == 0x0002
        assert res_off == 0

        e = copy.deepcopy(entry)
        e.stat.uid += 1
        res = ce_match_stat_basic(e, st)
        print(f"Cached uid changed: {describe_changes(res)}")
        assert res == OWNER_CHANGED == 0x0004

        e = copy.deepcopy(entry)
        e.stat.ino += 1
        res = ce_match_stat_basic(e, st)
        print(f"Cached inode changed: {describe_changes(res)}")
        assert res == INODE_CHANGED == 0x0010

        e = copy.deepcopy(entry)
        e.stat.dev += 1
        res_default = ce_match_stat_basic(e, st)
        res_stdev = ce_match_stat_basic(e, st, use_stdev=True)
        print(f"Cached device changed: {describe_changes(res_default)} in a default build, {describe_changes(res_stdev)} with USE_STDEV")
        assert res_default == 0
        assert res_stdev == INODE_CHANGED

        with open(path, "ab") as fh:
            fh.write(b"appended second line\n")
        os.utime(path, (t0, t0))
        res = ce_match_stat_basic(entry, os.lstat(path), **no_ctime)
        print(f"Appended bytes, mtime restored: {describe_changes(res)}")
        assert res == DATA_CHANGED == 0x0020
        dirty_st = os.lstat(path)
        res_basic = ce_match_stat_basic(entry, dirty_st, **no_ctime)

        write_file(path, base, t0)
        os.chmod(path, 0o755)
        res = ce_match_stat_basic(entry, os.lstat(path), **no_ctime)
        print(f"chmod +x: {describe_changes(res)}")
        assert res == MODE_CHANGED == 0x0008

        os.remove(path)
        os.symlink("elsewhere", path)
        res = ce_match_stat_basic(entry, os.lstat(path), **no_ctime)
        print(f"Regular file replaced by a symlink: TYPE_CHANGED set = {bool(res & TYPE_CHANGED)}")
        assert res & TYPE_CHANGED == TYPE_CHANGED == 0x0040
        os.remove(path)

        empty = os.path.join(work, "empty.txt")
        write_file(empty, b"", t0)
        smudged = IndexEntry(path="empty.txt", oid=blob_oid(b"not empty"), stat=StatData.from_os_stat(os.lstat(empty)))
        genuine = IndexEntry(path="empty.txt", oid=EMPTY_BLOB_OID, stat=StatData.from_os_stat(os.lstat(empty)))
        res_smudged = ce_match_stat_basic(smudged, os.lstat(empty))
        res_genuine = ce_match_stat_basic(genuine, os.lstat(empty))
        print(f"Cached size 0 on a non-empty blob: {describe_changes(res_smudged)}; on the empty blob: {describe_changes(res_genuine)}")
        assert res_smudged == DATA_CHANGED
        assert res_genuine == 0

        valid = copy.deepcopy(entry)
        valid.assume_valid = True
        res_valid = ie_match_stat(valid, dirty_st, path, 0, **no_ctime)
        res_plain = ie_match_stat(entry, dirty_st, path, 0, **no_ctime)
        print(f"Dirty file via ie_match_stat: {describe_changes(res_plain)}; with CE_VALID set: {describe_changes(res_valid)}")
        print(f"ce_match_stat_basic ignores CE_VALID: {describe_changes(res_basic)}")
        assert res_valid == 0
        assert res_plain == DATA_CHANGED
        assert res_basic == DATA_CHANGED

        skip = copy.deepcopy(entry)
        skip.skip_worktree = True
        skip.assume_valid = False
        res_skip = ie_match_stat(skip, dirty_st, path, 0, **no_ctime)
        intent = copy.deepcopy(entry)
        intent.intent_to_add = True
        res_intent = ie_match_stat(intent, dirty_st, path, 0, **no_ctime)
        print(f"skip-worktree entry: {describe_changes(res_skip)}; intent-to-add entry: {describe_changes(res_intent)}")
        assert res_skip == 0
        assert res_intent == DATA_CHANGED | TYPE_CHANGED | MODE_CHANGED == 0x0068

        sizes = (munge_st_size(5), munge_st_size((1 << 32) + 5), munge_st_size(1 << 32), munge_st_size(0))
        print(f"Cached size for 5 B, 4 GiB + 5 B, 4 GiB, 0 B: {list(sizes)}")
        assert sizes == (5, 5, 0x80000000, 0)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n=== Step 6: Racy Git Detection and Size Smudging ===")
    work = tempfile.mkdtemp(prefix="git_index_racy_")
    try:
        t0 = 1700000100  # index mtime second; the file is forced into the same second
        before = b"AAAA\n"
        after = b"BBBB\n"
        racy_path = os.path.join(work, "racy.txt")
        write_file(racy_path, before, t0)
        racy = entry_for(racy_path, before, "racy.txt")
        print(f"Staged 5 bytes with mtime == index mtime: racy = {is_racy_timestamp(racy, t0)}")
        assert is_racy_timestamp(racy, t0) is True

        res = ie_match_stat(racy, os.lstat(racy_path), racy_path, t0, **no_ctime)
        print(f"Unedited racy entry, content re-hashed: {describe_changes(res)}")
        assert res == 0

        with open(racy_path, "r+b") as fh:
            fh.write(after)
        os.utime(racy_path, (t0, t0))
        res_stat = ce_match_stat_basic(racy, os.lstat(racy_path), **no_ctime)
        res_full = ie_match_stat(racy, os.lstat(racy_path), racy_path, t0, **no_ctime)
        print(f"Same-size in-place edit, stat only: {describe_changes(res_stat)} (falsely clean)")
        print(f"Same edit with the racy content check: {describe_changes(res_full)}")
        assert res_stat == 0
        assert res_full == DATA_CHANGED

        unsmudged = copy.deepcopy(racy)
        smudged = copy.deepcopy(racy)
        did = smudge_racily_clean_entry(smudged, racy_path, t0, **no_ctime)
        print(f"Smudge the edited racy entry: {did}, cached size {smudged.stat.file_size} B (was {unsmudged.stat.file_size} B)")
        assert did is True and smudged.stat.file_size == 0

        later = t0 + 10
        res_unsmudged = ie_match_stat(unsmudged, os.lstat(racy_path), racy_path, later, **no_ctime)
        res_smudged = ie_match_stat(smudged, os.lstat(racy_path), racy_path, later, **no_ctime)
        print(f"Index timestamp moved to T+10, unsmudged entry: {describe_changes(res_unsmudged)} (the hole)")
        print(f"Index timestamp moved to T+10, smudged entry:   {describe_changes(res_smudged)}")
        assert res_unsmudged == 0
        assert res_smudged == DATA_CHANGED

        keep_path = os.path.join(work, "keep.txt")
        write_file(keep_path, before, t0)
        keep = entry_for(keep_path, before, "keep.txt")
        did = smudge_racily_clean_entry(keep, keep_path, t0, **no_ctime)
        print(f"Smudge an unchanged racy entry: {did}, cached size {keep.stat.file_size} B")
        assert did is False and keep.stat.file_size == len(before)

        grown_path = os.path.join(work, "grown.txt")
        write_file(grown_path, before, t0)
        grown = entry_for(grown_path, before, "grown.txt")
        with open(grown_path, "ab") as fh:
            fh.write(b"more\n")
        os.utime(grown_path, (t0, t0))
        did = smudge_racily_clean_entry(grown, grown_path, t0, **no_ctime)
        print(f"Racy entry whose stat data already differs: smudged = {did}, cached size {grown.stat.file_size} B")
        assert did is False and grown.stat.file_size == len(before)

        old_path = os.path.join(work, "old.txt")
        write_file(old_path, before, t0 - 50)
        old = entry_for(old_path, before, "old.txt")
        with open(old_path, "r+b") as fh:
            fh.write(after)
        os.utime(old_path, (t0 - 50, t0 - 50))
        did = smudge_racily_clean_entry(old, old_path, t0, **no_ctime)
        print(f"Edited entry older than the index (racy = {is_racy_timestamp(old, t0)}): smudged = {did}")
        assert did is False and is_racy_timestamp(old, t0) is False
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n=== Step 7: Index Extensions (TREE and REUC) ===")
    idx_ext = GitIndex(version=2)
    idx_ext.add_entry(IndexEntry(path="src/main.c", oid=hashlib.sha1(b"main").digest()))
    idx_ext.add_entry(IndexEntry(path="src/utils.c", oid=hashlib.sha1(b"utils").digest()))

    # Add cache-tree extension
    root_tree_oid = hashlib.sha1(b"tree root").digest()
    src_tree_oid = hashlib.sha1(b"tree src").digest()
    idx_ext.cache_tree = [
        CacheTreeNode(path="", entry_count=2, subtree_count=1, oid=root_tree_oid),
        CacheTreeNode(path="src", entry_count=2, subtree_count=0, oid=src_tree_oid),
        CacheTreeNode(path="invalidated_dir", entry_count=-1, subtree_count=0, oid=None),
    ]

    # Add resolve-undo extension
    oid_s1 = hashlib.sha1(b"anc").digest()
    oid_s2 = hashlib.sha1(b"our").digest()
    oid_s3 = hashlib.sha1(b"the").digest()
    idx_ext.resolve_undo = [
        ResolveUndoEntry(
            path="conflict_resolved.txt",
            stages={1: (MODE_REGULAR, oid_s1), 2: (MODE_REGULAR, oid_s2), 3: (MODE_REGULAR, oid_s3)},
        )
    ]

    raw_ext = idx_ext.serialize()
    parsed_ext = GitIndex.parse(raw_ext)

    print(f"Parsed cache-tree nodes: {len(parsed_ext.cache_tree)}")
    for node in parsed_ext.cache_tree:
        oid_hex = node.oid.hex()[:8] if node.oid else "None"
        print(f"  path='{node.path}' entries={node.entry_count} subtrees={node.subtree_count} oid={oid_hex}")
    assert len(parsed_ext.cache_tree) == 3
    assert parsed_ext.cache_tree[2].entry_count == -1

    print(f"Parsed resolve-undo records: {len(parsed_ext.resolve_undo)}")
    ru = parsed_ext.resolve_undo[0]
    print(f"  path='{ru.path}' stages={sorted(ru.stages.keys())}")
    assert ru.path == "conflict_resolved.txt"
    assert set(ru.stages.keys()) == {1, 2, 3}

    print("\n=== Step 8: Corruption and Tamper Detection ===")
    # 1. Byte tampering (trailer checksum failure)
    tampered_bytes = bytearray(raw_v2)
    tampered_bytes[20] ^= 0xFF
    try:
        GitIndex.parse(bytes(tampered_bytes))
        raise AssertionError("Failed to detect tampered byte")
    except ValueError as e:
        print(f"Checksum mismatch caught: {e}")

    # 2. Magic signature corruption
    bad_magic = bytearray(raw_v2)
    bad_magic[0:4] = b"DIRX"
    # Recompute trailing checksum so magic check is tested
    bad_magic[-20:] = hashlib.sha1(bad_magic[:-20]).digest()
    try:
        GitIndex.parse(bytes(bad_magic))
        raise AssertionError("Failed to detect bad magic")
    except ValueError as e:
        print(f"Bad magic caught: {e}")

    # 3. Out-of-order entries (the lab always checks; Git checks only when
    #    verify_ce_order is set, as git fsck does)
    unsorted = GitIndex(version=2)
    unsorted.entries.append(IndexEntry(path="zeta.c", oid=hashlib.sha1(b"z").digest()))
    unsorted.entries.append(IndexEntry(path="alpha.c", oid=hashlib.sha1(b"a").digest()))
    try:
        GitIndex.parse(unsorted.serialize())
        raise AssertionError("Failed to detect out-of-order entries")
    except ValueError as e:
        print(f"Out-of-order entries caught: {e}")

    # 4. Duplicate (path, stage) pair
    dup = GitIndex(version=2)
    dup.entries.append(IndexEntry(path="same.c", oid=hashlib.sha1(b"1").digest()))
    dup.entries.append(IndexEntry(path="same.c", oid=hashlib.sha1(b"2").digest()))
    try:
        GitIndex.parse(dup.serialize())
        raise AssertionError("Failed to detect duplicate entries")
    except ValueError as e:
        print(f"Duplicate entries caught: {e}")

    # 5. Unknown extensions: lowercase first byte is mandatory, uppercase optional
    base = GitIndex(version=2)
    base.add_entry(IndexEntry(path="x.c", oid=hashlib.sha1(b"x").digest()))
    base.extra_extensions = [(b"ABCD", b"optional body")]
    parsed_optional = GitIndex.parse(base.serialize())
    print(f"Unknown optional extension ABCD skipped: {len(parsed_optional.entries)} entry parsed")
    assert len(parsed_optional.entries) == 1
    base.extra_extensions = [(b"abcd", b"mandatory body")]
    try:
        GitIndex.parse(base.serialize())
        raise AssertionError("Failed to reject unknown mandatory extension")
    except ValueError as e:
        print(f"Unknown mandatory extension caught: {e}")
    print("Integrity verification completed: all corruption checks passed.")

    print("\n=== Step 9: Indexes Written by Real Git 2.55.0 ===")
    for label, hex_blob, version in (
        ("v2", REAL_GIT_INDEX_V2_HEX, 2),
        ("v4", REAL_GIT_INDEX_V4_HEX, 4),
    ):
        raw = bytes.fromhex("".join(hex_blob))
        real = GitIndex.parse(raw)
        assert real.version == version
        print(f"{label} fixture: {len(raw)} bytes, entries={len(real.entries)}, trailer checksum OK")
        for ent in real.entries:
            print(f"  {ent.path} mode={oct(ent.stat.mode)} size={ent.stat.file_size}")
        assert [ent.path for ent in real.entries] == ["a", "src/main.c", "src/util.c"]
        print(f"  cache-tree: {[(n.path, n.entry_count, n.subtree_count) for n in real.cache_tree]}")
        rebuilt = GitIndex(version=version)
        rebuilt.entries = real.entries
        rebuilt.cache_tree = real.cache_tree
        same = rebuilt.serialize() == raw
        print(f"  lab serializer reproduces Git's bytes exactly: {same}")
        assert same

    reuc_raw = bytes.fromhex("".join(REAL_GIT_INDEX_REUC_HEX))
    real_reuc = GitIndex.parse(reuc_raw)
    ru = real_reuc.resolve_undo[0]
    print(f"reuc fixture: {len(reuc_raw)} bytes, entries={len(real_reuc.entries)}, resolve-undo path={ru.path} stages={sorted(ru.stages)}")
    assert (ru.path, sorted(ru.stages)) == ("conflict.c", [1, 2, 3])
    assert all(mode == MODE_REGULAR for mode, _ in ru.stages.values())
    rebuilt = GitIndex(version=2)
    rebuilt.entries = real_reuc.entries
    rebuilt.cache_tree = real_reuc.cache_tree
    rebuilt.resolve_undo = real_reuc.resolve_undo
    same = rebuilt.serialize() == reuc_raw
    print(f"  lab serializer reproduces Git's bytes exactly: {same}")
    assert same


if __name__ == "__main__":
    run_lab()
