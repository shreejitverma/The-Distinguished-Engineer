#!/usr/bin/env python3
"""Verification script for Git LFS filter protocol and Batch API demonstration."""

import hashlib
import json
import os
import shlex
import subprocess
import sys


def run_git(repo_dir, *args):
    """Run a git command in the test repository and return stripped stdout."""
    res = subprocess.run(
        ["git"] + list(args),
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def run_git_status(repo_dir, *args):
    """Run git and return (returncode, stderr) without raising."""
    res = subprocess.run(["git"] + list(args), cwd=repo_dir, capture_output=True, text=True)
    return res.returncode, res.stderr


def check_batch_request(req):
    """Reject a Batch API request that lacks a field required by docs/api/batch.md."""
    assert req["operation"] in ("download", "upload"), "operation must be download or upload"
    assert isinstance(req["objects"], list) and req["objects"], "objects must be a non-empty list"
    for obj in req["objects"]:
        assert len(obj["oid"]) == 64 and isinstance(obj["size"], int), "object needs a 64-hex oid and integer size"


def check_batch_response(resp, operation):
    """Reject a Batch API response whose actions do not match the requested operation."""
    for obj in resp["objects"]:
        action = obj["actions"][operation]
        assert action["href"].startswith("https://"), "action needs an https href"
        assert isinstance(action["expires_in"], int), "expires_in must be an integer"


def must_reject(check, *args):
    """Return True when the schema check raises, which is what a negative case must do."""
    try:
        check(*args)
    except (AssertionError, KeyError):
        return True
    return False


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <repo_dir> <filter_script>")
        sys.exit(1)

    repo_dir = os.path.abspath(sys.argv[1])
    filter_script = os.path.abspath(sys.argv[2])

    print("=== Git LFS Long-Running Filter and Batch API Verification ===")

    # 1. Initialize repository and configure filter
    run_git(repo_dir, "init", "-b", "main", "-q")
    run_git(repo_dir, "config", "filter.lfsdemo.process", f"python3 {shlex.quote(filter_script)}")
    run_git(repo_dir, "config", "filter.lfsdemo.required", "true")

    # 2. Configure .gitattributes
    gitattributes_path = os.path.join(repo_dir, ".gitattributes")
    with open(gitattributes_path, "w", encoding="ascii") as f:
        f.write("*.bin filter=lfsdemo -text\n")

    # 3. Create synthetic 64 KiB binary asset
    raw_pattern = b"WEIGHTS_TENSOR_BLOCK_V1:" + bytes(range(232))
    raw_payload = raw_pattern * 256  # exactly 65,536 bytes
    expected_size = len(raw_payload)
    expected_sha256 = hashlib.sha256(raw_payload).hexdigest()

    asset_path = os.path.join(repo_dir, "weights.bin")
    with open(asset_path, "wb") as f:
        f.write(raw_payload)

    print(f"Original working tree asset size: {expected_size} bytes")
    print(f"Original asset SHA-256: {expected_sha256}")

    # 4. Stage and commit through clean filter
    run_git(repo_dir, "add", ".gitattributes", "weights.bin")
    run_git(repo_dir, "commit", "-m", "Commit binary model weights via LFS filter", "-q")

    # 5. Inspect Git object database (Index & Tree)
    staged_pointer = run_git(repo_dir, "cat-file", "-p", ":weights.bin")
    pointer_blob_size = int(run_git(repo_dir, "cat-file", "-s", ":weights.bin"))
    ls_tree_entry = run_git(repo_dir, "ls-tree", "HEAD", "weights.bin")

    print("\n--- Git Object Database Inspection ---")
    print(f"Git object type and mode: {ls_tree_entry.split()[0]} {ls_tree_entry.split()[1]}")
    print(f"Git stored blob size: {pointer_blob_size} bytes (raw binary was {expected_size} bytes)")
    print("Stored pointer content:")
    for line in staged_pointer.splitlines():
        print(f"  {line}")

    # Validate pointer structure against Git LFS v1 specification
    pointer_lines = staged_pointer.splitlines()
    assert len(pointer_lines) == 3, "Pointer must contain exactly three lines"
    assert pointer_lines[0] == "version https://git-lfs.github.com/spec/v1"
    assert pointer_lines[1] == f"oid sha256:{expected_sha256}"
    assert pointer_lines[2] == f"size {expected_size}"
    assert pointer_blob_size < 1024, "LFS pointer must be under 1024 bytes"
    print("Pointer schema validation: PASS (version, sorted oid/size, <1024 B)")

    # Repack so that loose objects and packfiles are both covered, then bound every object's size.
    run_git(repo_dir, "repack", "-a", "-d", "-q")
    sizes = run_git(repo_dir, "cat-file", "--batch-all-objects", "--batch-check=%(objectsize)").split()
    largest = max(int(n) for n in sizes)
    assert largest < 1024, f"an object of {largest} bytes reached the object database"
    print(f"Largest object after repack: {largest} bytes (limit 1024, raw binary was {expected_size} bytes)")

    # 6. Verify Content-Addressable Storage (CAS) on filesystem
    cas_path = os.path.join(
        repo_dir,
        ".git",
        "lfs",
        "objects",
        expected_sha256[:2],
        expected_sha256[2:4],
        expected_sha256,
    )
    assert os.path.isfile(cas_path), f"CAS file missing at {cas_path}"
    cas_size = os.path.getsize(cas_path)
    with open(cas_path, "rb") as f:
        cas_sha256 = hashlib.sha256(f.read()).hexdigest()

    print("\n--- Content-Addressable Storage (CAS) Inspection ---")
    rel_cas = os.path.relpath(cas_path, repo_dir)
    print(f"CAS location: {rel_cas}")
    print(f"CAS file size: {cas_size} bytes")
    print(f"CAS SHA-256 match: {cas_sha256 == expected_sha256}")

    # 7. Test Smudge Filter / Working Tree Restoration
    os.remove(asset_path)
    assert not os.path.exists(asset_path), "File deletion failed"
    run_git(repo_dir, "checkout", "--", "weights.bin")

    with open(asset_path, "rb") as f:
        restored_payload = f.read()
    restored_size = len(restored_payload)
    restored_sha256 = hashlib.sha256(restored_payload).hexdigest()

    print("\n--- Smudge Filter Restoration ---")
    print(f"Restored working tree size: {restored_size} bytes")
    print(f"Restored SHA-256 match: {restored_sha256 == expected_sha256}")
    assert restored_payload == raw_payload, "Restored payload does not match original"

    # A pointer whose object is missing from the store must fail the checkout, not restore the pointer text.
    os.remove(cas_path)
    os.remove(asset_path)
    code, _ = run_git_status(repo_dir, "checkout", "--", "weights.bin")
    assert code != 0, "checkout succeeded although the CAS object was missing"
    assert not os.path.exists(asset_path), "a file was written although smudge failed"
    print("Smudge with a missing CAS object: checkout fails and writes no file")

    # 8. Git LFS Batch API Client/Server Wire Schema Verification
    print("\n--- Git LFS Batch API Wire Schema Verification ---")
    batch_req = {
        "operation": "download",
        "transfers": ["basic"],
        "ref": {"name": "refs/heads/main"},
        "objects": [
            {
                "oid": expected_sha256,
                "size": expected_size,
            }
        ],
        "hash_algo": "sha256",
    }
    req_json = json.dumps(batch_req, indent=2, sort_keys=True)
    print("Batch API Request payload:")
    for line in req_json.splitlines():
        print(f"  {line}")

    # Validate server response structure with pre-signed URL action
    batch_resp = {
        "transfer": "basic",
        "objects": [
            {
                "oid": expected_sha256,
                "size": expected_size,
                "authenticated": True,
                "actions": {
                    "download": {
                        "href": f"https://storage.blob.core.windows.net/lfs-store/{expected_sha256}",
                        "header": {
                            "Authorization": "Bearer signed-token-7f9a2",
                            "Accept": "application/octet-stream",
                        },
                        "expires_in": 86400,
                    }
                },
            }
        ],
    }
    resp_json = json.dumps(batch_resp, indent=2, sort_keys=True)
    print("Batch API Response payload:")
    for line in resp_json.splitlines():
        print(f"  {line}")

    # Schema checks: the valid pair passes, and each broken copy is rejected by the same validators.
    check_batch_request(batch_req)
    check_batch_response(batch_resp, "download")
    assert batch_resp["transfer"] == "basic"
    action = batch_resp["objects"][0]["actions"]["download"]
    assert action["href"].endswith(expected_sha256)
    assert action["expires_in"] == 86400
    bad_op = {**batch_req, "operation": "delete"}
    bad_oid = {**batch_req, "objects": [{"oid": "abc", "size": expected_size}]}
    bad_resp = {"objects": [{"actions": {"upload": action}}]}
    assert must_reject(check_batch_request, bad_op)
    assert must_reject(check_batch_request, bad_oid)
    assert must_reject(check_batch_response, bad_resp, "download")
    print("Batch API schema handshake: PASS (valid pair accepted, 3 broken cases rejected)")


if __name__ == "__main__":
    main()
