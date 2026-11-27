#!/usr/bin/env python3
"""Drill 1: Remote Action Cache Poisoning and Merkle Root Invalidation.

Simulates an REAPI v2 Content Addressable Storage (CAS) and Action Cache (AC).
Demonstrates how an unrecorded dependency or nondeterministic header creates
a cache key collision where a poisoned object file is served to clean workers,
and shows how cryptographic input-root Merkle verification detects the anomaly.
"""

import hashlib
import json
import sys

def sha256_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def make_merkle_root(files: dict) -> str:
    # Directory proto Merkle root: sorted filenames and their blob digests
    sorted_items = sorted(files.items())
    manifest = "".join(f"{name}:{sha256_digest(data.encode('utf-8'))}\n" for name, data in sorted_items)
    return sha256_digest(manifest.encode("utf-8"))

def compute_action_digest(command: list, input_root: str, env: dict) -> str:
    # Action proto: command_digest + input_root_digest + sorted env
    cmd_bytes = json.dumps({"args": command, "env": sorted(env.items())}, sort_keys=True).encode("utf-8")
    cmd_digest = sha256_digest(cmd_bytes)
    action_bytes = f"cmd:{cmd_digest};root:{input_root}".encode("utf-8")
    return sha256_digest(action_bytes)

def main():
    print("[DRILL 1] Initializing Remote Execution Action Cache...")
    action_cache = {}
    cas_storage = {}

    # Legitimate source code
    source_clean = "int get_threshold() { return 1000; }\n"
    header_clean = "#define ENGINE_MAX_SAMPLES 64\n"
    inputs = {
        "engine.c": source_clean,
        "engine.h": header_clean
    }
    compile_cmd = ["gcc", "-O2", "-c", "engine.c", "-o", "engine.o"]
    clean_env = {"LC_ALL": "C", "PATH": "/usr/bin"}

    clean_root = make_merkle_root(inputs)
    clean_action_digest = compute_action_digest(compile_cmd, clean_root, clean_env)
    print(f"[DRILL 1] Legitimate Action Digest: {clean_action_digest[:16]}...")

    # Attacker / Rogue Worker with unrecorded host header
    # Worker produces tainted binary with altered constant (threshold 0)
    tainted_binary_bytes = b"\x7fELF_TAINTED_BINARY_PAYLOAD_OFFSET_ZERO\x00\x00"
    tainted_blob_hash = sha256_digest(tainted_binary_bytes)
    cas_storage[tainted_blob_hash] = tainted_binary_bytes

    # Rogue worker uploads tainted ActionResult under clean ActionDigest
    action_cache[clean_action_digest] = {
        "output_files": [{"path": "engine.o", "digest": tainted_blob_hash, "size": len(tainted_binary_bytes)}],
        "exit_code": 0,
        "builder_id": "worker-pool-us-east-412"
    }
    print("[DRILL 1] Poisoned ActionResult injected into Action Cache by unhardened worker.")

    # Clean CI Runner attempts to build
    print("[DRILL 1] Clean CI Runner querying Action Cache for build...")
    query_digest = compute_action_digest(compile_cmd, clean_root, clean_env)
    cached_result = action_cache.get(query_digest)

    if cached_result:
        cached_blob = cas_storage[cached_result["output_files"][0]["digest"]]
        print(f"[DRILL 1] ACTION_CACHE_HIT: Retrieved cached engine.o ({cached_result['output_files'][0]['digest'][:16]}...)")
        # In a naive build, the tainted object is linked, causing production segfault or failure
        if b"TAINTED" in cached_blob:
            print("[DRILL 1] DETECTED: Poisoned object file found in remote cache!")

    # Resolution & Enforcement: Cryptographic Action Provenance & Isolated Sandboxing
    print("[DRILL 1] Applying Fix: SLSA v1.2 Cryptographic Action Verification & Strict Sandboxing...")
    trusted_builders = {"worker-hardened-cluster-001", "worker-hardened-cluster-002"}
    
    # Validation gate
    builder = cached_result.get("builder_id", "")
    if builder not in trusted_builders:
        print(f"[DRILL 1] REJECTED: Builder '{builder}' lacks verified cryptographic provenance signature.")
        # Evict poisoned entry
        del action_cache[query_digest]
        print("[DRILL 1] EVICTED: Poisoned ActionDigest purged from remote cache.")

    # Rebuilding with hermetic container sandbox
    legit_binary_bytes = b"\x7fELF_LEGITIMATE_ENGINE_OBJECT_CODE\x00\x00"
    legit_blob_hash = sha256_digest(legit_binary_bytes)
    cas_storage[legit_blob_hash] = legit_binary_bytes
    action_cache[clean_action_digest] = {
        "output_files": [{"path": "engine.o", "digest": legit_blob_hash, "size": len(legit_binary_bytes)}],
        "exit_code": 0,
        "builder_id": "worker-hardened-cluster-001"
    }
    print("[DRILL 1] SUCCESS: Hermetic compilation restored clean cache entry.")

if __name__ == "__main__":
    main()
