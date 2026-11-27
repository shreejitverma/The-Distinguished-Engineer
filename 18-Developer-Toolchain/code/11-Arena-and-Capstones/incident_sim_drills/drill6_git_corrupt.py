#!/usr/bin/env python3
"""Drill 6: Git Corrupted Loose Object Recovery Under P0 Incident.

Simulates a disk bit-flip or truncated write corrupting a loose Git object
during an active production deployment.
Executes diagnostic detection using git fsck (or internal object verification)
and performs surgical object reconstruction to restore repository integrity.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import zlib

def compute_git_sha1(header_and_data: bytes) -> str:
    return hashlib.sha1(header_and_data).hexdigest()

def main():
    work_dir = tempfile.mkdtemp(prefix="drill6_git_")
    try:
        print("[DRILL 6] Initializing clean Git repository structure...")
        git_dir = os.path.join(work_dir, ".git")
        obj_root = os.path.join(git_dir, "objects")
        os.makedirs(obj_root, exist_ok=True)
        
        # Create hotfix payload
        hotfix_content = b"// P0 EMERGENCY HOTFIX: Null pointer guard in pricing dispatch\nint apply_hotfix() { return 1; }\n"
        header = f"blob {len(hotfix_content)}\0".encode("ascii")
        full_object = header + hotfix_content
        blob_hash = compute_git_sha1(full_object)
        print(f"[DRILL 6] Hotfix blob object SHA-1: {blob_hash}")
        
        # Write valid object
        obj_dir = os.path.join(obj_root, blob_hash[:2])
        os.makedirs(obj_dir, exist_ok=True)
        obj_file = os.path.join(obj_dir, blob_hash[2:])
        
        with open(obj_file, "wb") as f:
            f.write(zlib.compress(full_object))
        os.chmod(obj_file, 0o444)
        print(f"[DRILL 6] Valid Git loose object stored at objects/{blob_hash[:2]}/{blob_hash[2:]}")
        
        # Check if git binary is present on host
        has_git = shutil.which("git") is not None
        
        # Inject corruption: overwrite object file with corrupted bytes
        print(f"[DRILL 6] Injecting filesystem corruption into loose object: {blob_hash[:2]}/{blob_hash[2:]}...")
        os.chmod(obj_file, 0o644)
        with open(obj_file, "wb") as f:
            f.write(b"\x1f\x8b\x08GARBAGE_CORRUPTED_SECTOR_DATA\x00\x00\x00")
        os.chmod(obj_file, 0o444)
        
        # Diagnostic detection
        print("[DRILL 6] Executing diagnostic object integrity check...")
        corrupted = False
        try:
            with open(obj_file, "rb") as f:
                decompressed = zlib.decompress(f.read())
                computed_hash = compute_git_sha1(decompressed)
                if computed_hash != blob_hash:
                    corrupted = True
                    err_msg = f"sha1 mismatch {computed_hash} != {blob_hash}"
        except Exception as e:
            corrupted = True
            err_msg = f"inflate: data stream error ({str(e)})"
            
        if corrupted:
            print(f"[DRILL 6] FSCK_ERROR_DETECTED: error in loose object {blob_hash}: {err_msg}")
            
        # Emergency Recovery Procedure
        print("[DRILL 6] Applying Recovery: Reconstructing Git loose object from working copy...")
        recovered_object = header + hotfix_content
        recompressed = zlib.compress(recovered_object)
        
        os.chmod(obj_file, 0o644)
        with open(obj_file, "wb") as f:
            f.write(recompressed)
        os.chmod(obj_file, 0o444)
        print("[DRILL 6] Loose object reconstructed and re-compressed with zlib.")
        
        # Post-recovery integrity validation
        with open(obj_file, "rb") as f:
            decompressed = zlib.decompress(f.read())
            post_hash = compute_git_sha1(decompressed)
            
        if post_hash == blob_hash:
            print(f"[DRILL 6] POST_RECOVERY_CHECK: SHA-1 matches ({post_hash[:16]}...). Object valid.")
            print("[DRILL 6] SUCCESS: Git repository integrity restored! 0 errors detected.")
        else:
            print("[DRILL 6] RECOVERY_FAILED: SHA-1 mismatch after reconstruction!")
            sys.exit(1)
            
    finally:
        shutil.rmtree(work_dir)

if __name__ == "__main__":
    main()
