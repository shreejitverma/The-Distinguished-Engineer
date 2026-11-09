#!/usr/bin/env python3
"""Verification driver for Git Object Store and Content-Addressing Lab."""

import binascii
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zlib

from object_store import (
    HashMismatchError,
    ObjectStore,
    SizeMismatchError,
    TreeOrderError,
    MODE_DIR,
    MODE_REGULAR,
    MODE_EXECUTABLE,
    base_name_compare,
)


# Real git is the oracle for every claim of the form "matches Git".
# User and system config are disabled so core.* or init.* settings cannot change results.
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")


def git(repo, *args, stdin=None):
    """Run git in repo; return the CompletedProcess without raising."""
    try:
        return subprocess.run(
            ["git", "-C", repo, *args], input=stdin, capture_output=True, env=GIT_ENV, check=False
        )
    except FileNotFoundError:
        sys.exit("This lab needs git on PATH: it cross-checks every object against real git.")


def init_repo(path, object_format):
    os.makedirs(path)
    proc = git(path, "init", "-q", f"--object-format={object_format}")
    assert proc.returncode == 0, proc.stderr
    return path


def loose_oids(store):
    """Every loose object ID under the store, found by walking the fan-out directories."""
    found = []
    for fan in sorted(os.listdir(store.objects_dir)):
        if len(fan) == 2 and all(c in "0123456789abcdef" for c in fan):
            found += [fan + rest for rest in sorted(os.listdir(os.path.join(store.objects_dir, fan)))]
    return found


def run_lab():
    tmp_root = tempfile.mkdtemp(prefix="git_obj_lab_")
    try:
        print("=== Step 1: Loose Object Header Serialization and Hashing ===")
        sha1_root = init_repo(os.path.join(tmp_root, "sha1"), "sha1")
        store_sha1 = ObjectStore(sha1_root, object_format="sha1")

        # Empty blob
        empty_blob_oid = store_sha1.write_blob(b"")
        print(f"Empty blob SHA-1: {empty_blob_oid}")
        assert empty_blob_oid == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"

        # Empty tree
        empty_tree_oid = store_sha1.write_tree([])
        print(f"Empty tree SHA-1: {empty_tree_oid}")
        assert empty_tree_oid == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

        # Canonical "hello world\n" blob
        hw_content = b"hello world\n"
        hw_oid = store_sha1.write_blob(hw_content)
        print(f"'hello world\\n' blob SHA-1: {hw_oid} (size {len(hw_content)} B)")
        assert hw_oid == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"

        # Check loose object permissions
        hw_path = store_sha1.object_path(hw_oid)
        st_mode = stat.S_IMODE(os.stat(hw_path).st_mode)
        print(f"Loose object file permissions: {oct(st_mode)} (read-only 0444)")
        assert st_mode == 0o444

        # Read back raw object
        obj_type, payload = store_sha1.read_raw_object(hw_oid)
        print(f"Read back object type: {obj_type}, payload: {payload.decode().strip()!r}")
        assert obj_type == "blob" and payload == hw_content

        # Git's loose default is zlib level 1 (Z_BEST_SPEED): the stream starts 0x78 0x01
        with open(hw_path, "rb") as f:
            assert f.read(2) == b"\x78\x01", "loose object is not a level-1 zlib stream"
        print("Loose object zlib header: 78 01 (level 1, Git's loose default)")

        # Publishing must never replace an existing object (Git uses link(2), not rename(2)).
        before = open(hw_path, "rb").read()
        decoy = os.path.join(os.path.dirname(hw_path), "tmp_obj_decoy")
        with open(decoy, "wb") as f:
            f.write(b"not the real object")
        created = ObjectStore.publish_noclobber(decoy, hw_path)
        assert created is False and not os.path.exists(decoy)
        assert open(hw_path, "rb").read() == before, "existing object was overwritten"
        print("Publish over an existing object: kept the original, temp file removed")

        print("\n=== Step 2: Binary Tree Serialization and Directory Sorting ===")
        # Demonstrating the canonical base_name_compare rule
        # Items: 'bin' (dir), 'bin-tool' (file), 'bin.c' (file), 'bina' (file)
        blob_a = store_sha1.write_blob(b"tool")
        blob_b = store_sha1.write_blob(b"int main() {}")
        blob_c = store_sha1.write_blob(b"payload")
        sub_tree_oid = empty_tree_oid

        tree_entries = [
            (MODE_DIR, "bin", sub_tree_oid),
            (MODE_REGULAR, "bin-tool", blob_a),
            (MODE_REGULAR, "bin.c", blob_b),
            (MODE_REGULAR, "bina", blob_c),
        ]

        canonical_tree_oid = store_sha1.write_tree(tree_entries)
        print(f"Canonical tree OID: {canonical_tree_oid}")

        # Parse canonical tree back
        _, tree_payload = store_sha1.read_raw_object(canonical_tree_oid)
        parsed_entries = store_sha1.parse_tree(tree_payload)
        print("Canonical tree entry order:")
        for mode, name, oid, ttype in parsed_entries:
            print(f"  {mode:06o} {ttype} {oid[:8]}... {name}")

        expected_order = ["bin-tool", "bin.c", "bin", "bina"]
        actual_order = [e[1] for e in parsed_entries]
        assert actual_order == expected_order, f"Order mismatch: {actual_order}"
        print("Tree entries correctly ordered via implicit trailing slash rule.")

        # Demonstrate rejection of naive sorting
        naive_sorted = sorted(tree_entries, key=lambda x: x[1])
        naive_payload = bytearray()
        for mode, name, oid in naive_sorted:
            naive_payload.extend(f"{mode:o} {name}\0".encode("ascii"))
            naive_payload.extend(binascii.unhexlify(oid))

        # The naive tree lives in its own repository so it cannot taint the good one.
        bad_root = init_repo(os.path.join(tmp_root, "naive"), "sha1")
        naive_oid = ObjectStore(bad_root).write_raw_object("tree", bytes(naive_payload))
        print(f"Naive sorted tree OID: {naive_oid} (differs from canonical)")
        assert naive_oid != canonical_tree_oid

        try:
            store_sha1.parse_tree(bytes(naive_payload))
            print("ERROR: naive tree was not rejected!")
            sys.exit(1)
        except TreeOrderError as e:
            print(f"Strict parser caught unsorted tree: {e}")

        print("\n=== Step 3: Full Merkle DAG Construction and Traversal ===")
        # Commit 1: Initial commit with README and src/
        readme_v1 = store_sha1.write_blob(b"# Example Project\nVersion 1.0\n")
        main_v1 = store_sha1.write_blob(b"int main() { return 0; }\n")

        src_tree_v1 = store_sha1.write_tree([
            (MODE_REGULAR, "main.c", main_v1),
        ])
        root_tree_v1 = store_sha1.write_tree([
            (MODE_REGULAR, "README.md", readme_v1),
            (MODE_DIR, "src", src_tree_v1),
        ])

        commit_v1 = store_sha1.write_commit(
            tree_oid=root_tree_v1,
            parent_oids=[],
            author_name="Alice Engineer",
            author_email="alice@example.com",
            author_epoch=1731000000,
            author_tz="+0000",
            committer_name="Alice Engineer",
            committer_email="alice@example.com",
            committer_epoch=1731000000,
            committer_tz="+0000",
            message="Initial project commit\n",
        )
        print(f"Root Commit OID: {commit_v1}")
        print(f"  Root Tree OID: {root_tree_v1}")
        print(f"  src/ Tree OID: {src_tree_v1}")
        print(f"  README blob:   {readme_v1}")

        # Commit 2: Update main.c to Version 2.0 (README remains identical)
        main_v2 = store_sha1.write_blob(b"int main() { return 42; }\n")
        src_tree_v2 = store_sha1.write_tree([
            (MODE_REGULAR, "main.c", main_v2),
        ])
        root_tree_v2 = store_sha1.write_tree([
            (MODE_REGULAR, "README.md", readme_v1), # Reused unchanged blob!
            (MODE_DIR, "src", src_tree_v2),
        ])

        commit_v2 = store_sha1.write_commit(
            tree_oid=root_tree_v2,
            parent_oids=[commit_v1],
            author_name="Bob Developer",
            author_email="bob@example.com",
            author_epoch=1731003600,
            author_tz="+0000",
            committer_name="Bob Developer",
            committer_email="bob@example.com",
            committer_epoch=1731003600,
            committer_tz="+0000",
            message="Update main return value\n",
        )
        print(f"Child Commit OID: {commit_v2}")
        print(f"  Parent Commit:  {commit_v1}")
        print(f"  Root Tree OID:  {root_tree_v2} (changed)")
        print(f"  src/ Tree OID:  {src_tree_v2} (changed)")
        print(f"  README blob:    {readme_v1} (deduplicated, unchanged)")
        readme_oids = []
        for tree_oid in (root_tree_v1, root_tree_v2):
            _, body = store_sha1.read_raw_object(tree_oid)
            readme_oids.append({e[1]: e[2] for e in store_sha1.parse_tree(body)}["README.md"])
        assert readme_oids == [readme_v1, readme_v1], "README.md was not shared between the root trees"
        print("README.md entry OID is identical in both root trees: yes")

        # Annotated Tag
        tag_v1 = store_sha1.write_tag(
            target_oid=commit_v2,
            target_type="commit",
            tag_name="v2.0",
            tagger_name="Release Lead",
            tagger_email="lead@example.com",
            tagger_epoch=1731007200,
            tagger_tz="+0000",
            message="Production Release 2.0\n",
        )
        print(f"Annotated Tag OID: {tag_v1}")
        _, tag_payload = store_sha1.read_raw_object(tag_v1)
        parsed_tag = store_sha1.parse_tag(tag_payload)
        print(f"  Points to: {parsed_tag['type']} {parsed_tag['object']}")
        print(f"  Tag Name:  {parsed_tag['tag']}")

        print("\n=== Step 4: Corruption and Tamper Detection ===")
        # Damaged objects live in their own repository so the good one stays fsck-clean.
        corrupt_root = init_repo(os.path.join(tmp_root, "corrupt"), "sha1")
        store_bad = ObjectStore(corrupt_root, object_format="sha1")

        # 1. Bit-flip in compressed loose object
        with open(store_sha1.object_path(commit_v1), "rb") as f:
            raw_comp = bytearray(f.read())
        raw_comp[15] ^= 0xFF
        bad_oid = "ffffffffffffffffffffffffffffffffffffffff"
        bad_path = store_bad.object_path(bad_oid)
        os.makedirs(os.path.dirname(bad_path), exist_ok=True)
        with open(bad_path, "wb") as f:
            f.write(raw_comp)

        try:
            store_bad.read_raw_object(bad_oid)
            print("ERROR: Bit-flip was not caught!")
            sys.exit(1)
        except zlib.error as e:
            print(f"Bit-flip caught by zlib decompress: {type(e).__name__}")

        # 2. Valid zlib stream, right size, wrong ID: only the hash can catch it
        fake_content = b"blob 12\0hello hacker"
        tamper_oid = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        tamper_path = store_bad.object_path(tamper_oid)
        os.makedirs(os.path.dirname(tamper_path), exist_ok=True)
        with open(tamper_path, "wb") as f:
            f.write(zlib.compress(fake_content, 1))

        try:
            store_bad.read_raw_object(tamper_oid)
            print("ERROR: Tampered hash was not caught!")
            sys.exit(1)
        except HashMismatchError:
            print("Content tampering caught by OID verification: hash mismatch")

        # 3. Header declares 5 bytes but 12 follow: the size check must fire before hashing
        size_oid = "dddddddddddddddddddddddddddddddddddddddd"
        size_path = store_bad.object_path(size_oid)
        os.makedirs(os.path.dirname(size_path), exist_ok=True)
        with open(size_path, "wb") as f:
            f.write(zlib.compress(b"blob 5\0hello hacker", 1))

        try:
            store_bad.read_raw_object(size_oid)
            print("ERROR: Size mismatch was not caught!")
            sys.exit(1)
        except SizeMismatchError:
            print("Header size lie caught before hashing: size mismatch")

        print("\n=== Step 5: Cryptographic Hash Transition: SHA-1 vs SHA-256 ===")
        sha256_root = init_repo(os.path.join(tmp_root, "sha256"), "sha256")
        store_sha256 = ObjectStore(sha256_root, object_format="sha256")

        s256_empty_blob = store_sha256.write_blob(b"")
        s256_empty_tree = store_sha256.write_tree([])
        s256_hw_blob = store_sha256.write_blob(hw_content)

        print(f"SHA-256 Empty blob: {s256_empty_blob}")
        print(f"SHA-256 Empty tree: {s256_empty_tree}")
        print(f"SHA-256 'hello world\\n' blob: {s256_hw_blob}")
        assert s256_empty_blob == "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813"
        assert s256_empty_tree == "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321"
        assert s256_hw_blob == "0bd69098bd9b9cc5934a610ab65da429b525361147faa7b5b922919e9a23143d"

        # Tree entry comparison
        tree256_entries = [(MODE_REGULAR, "hello.txt", s256_hw_blob)]
        s256_tree = store_sha256.write_tree(tree256_entries)
        _, s256_tree_body = store_sha256.read_raw_object(s256_tree)
        parsed_s256 = store_sha256.parse_tree(s256_tree_body)

        print(f"SHA-256 Tree OID: {s256_tree} (size {len(s256_tree_body)} B)")
        print(f"  Entry: {parsed_s256[0][0]:06o} {parsed_s256[0][3]} {parsed_s256[0][2][:12]}... {parsed_s256[0][1]}")

        # Tree entry size difference, measured from the stored payloads (not hardcoded in the output):
        # '100644 hello.txt\0' is 17 B, plus the binary OID.
        s1_tree = store_sha1.write_tree([(MODE_REGULAR, "hello.txt", hw_oid)])
        _, s1_tree_body = store_sha1.read_raw_object(s1_tree)
        assert len(s1_tree_body) == 17 + 20 and len(s256_tree_body) == 17 + 32
        delta = len(s256_tree_body) - len(s1_tree_body)
        assert delta == 12
        print(f"SHA-1 single-file tree size:   {len(s1_tree_body)} B (20 B OID)")
        print(f"SHA-256 single-file tree size: {len(s256_tree_body)} B (32 B OID, +{delta} B overhead)")

        print("\n=== Step 6: Cross-check Against Real Git ===")
        # git fsck re-hashes every loose object against its file name and parses each type strictly.
        proc = git(sha1_root, "fsck", "--strict", "--no-dangling")
        assert proc.returncode == 0, proc.stderr.decode()
        print("git fsck --strict on the SHA-1 store: clean")

        oids = loose_oids(store_sha1)
        for oid in oids:
            obj_type, payload = store_sha1.read_raw_object(oid)
            assert git(sha1_root, "cat-file", "-t", oid).stdout.decode().strip() == obj_type
            assert git(sha1_root, "cat-file", obj_type, oid).stdout == payload
        print(f"git cat-file agrees on type and bytes for all {len(oids)} loose objects")

        proc = git(sha1_root, "hash-object", "--stdin", stdin=hw_content)
        assert proc.stdout.decode().strip() == hw_oid
        listing = "".join(
            f"{mode:06o} {ttype} {oid}\t{name}\n" for mode, name, oid, ttype in parsed_entries
        )
        mktree = git(sha1_root, "mktree", stdin=listing.encode())
        assert mktree.stdout.decode().strip() == canonical_tree_oid, mktree.stderr.decode()
        print("git hash-object and git mktree reproduce the lab's blob and canonical tree OIDs")

        proc = git(bad_root, "fsck", "--strict", "--no-dangling")
        assert proc.returncode != 0 and b"treeNotSorted" in proc.stdout + proc.stderr
        print("git fsck rejects the naive-sorted tree: treeNotSorted")

        # Git does not re-hash on read: cat-file rejects only the broken zlib stream.
        # The wrong-ID and size-lie objects are found by fsck, which re-hashes every file.
        assert git(corrupt_root, "cat-file", "-p", bad_oid).returncode != 0
        proc = git(corrupt_root, "fsck", "--no-dangling")
        report = (proc.stdout + proc.stderr).decode()
        assert proc.returncode != 0
        for lie_oid in (tamper_oid, size_oid):
            assert f"hash-path mismatch, found at: .git/objects/{lie_oid[:2]}/{lie_oid[2:]}" in report, report
        print("git cat-file rejects the bit-flip; git fsck reports hash-path mismatch for the other 2 damaged objects")

        proc = git(sha256_root, "fsck", "--strict", "--no-dangling")
        assert proc.returncode == 0, proc.stderr.decode()
        proc = git(sha256_root, "hash-object", "--stdin", stdin=hw_content)
        assert proc.stdout.decode().strip() == s256_hw_blob
        print("git fsck --strict on the SHA-256 store: clean, and hash-object agrees on the blob OID")

        print("\nAll object model and content addressing checks passed successfully.")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    run_lab()
