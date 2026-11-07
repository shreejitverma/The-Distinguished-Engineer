import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

def make_pkt(payload: bytes) -> bytes:
    """Encode bytes into a Git pkt-line framing with 4-byte hex prefix."""
    return f"{len(payload) + 4:04x}".encode("ascii") + payload

def parse_pkt_stream(stream):
    """Read packets from stream until flush (0000) or EOF."""
    pkts = []
    while True:
        hdr = stream.read(4)
        if not hdr or len(hdr) < 4:
            break
        if hdr == b"0000":
            pkts.append(("FLUSH", b""))
            break
        if hdr == b"0001":
            pkts.append(("DELIM", b""))
            continue
        if hdr == b"0002":
            pkts.append(("END", b""))
            break
        try:
            length = int(hdr, 16)
        except ValueError:
            break
        if length < 4:
            break
        payload = stream.read(length - 4)
        pkts.append(("DATA", payload))
    return pkts

MIN_GIT = (2, 49)  # git backfill first shipped in Git 2.49.0


def require_git(minimum):
    """Exit with a clear error unless the installed Git is at least `minimum`."""
    out = subprocess.check_output(["git", "--version"], text=True).strip()
    m = re.match(r"git version (\d+)\.(\d+)", out)
    if not m or (int(m.group(1)), int(m.group(2))) < minimum:
        sys.exit(f"error: needs Git >= {minimum[0]}.{minimum[1]} (git backfill); found: {out}")


def count_trace(path, needle):
    """Count trace lines containing `needle`; 0 when git wrote no trace at all."""
    if not os.path.exists(path):
        return 0
    with open(path, errors="replace") as f:
        return sum(1 for line in f if needle in line)


def main():
    require_git(MIN_GIT)
    tmp = tempfile.mkdtemp(prefix="partial_clone_lab_")
    try:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_AUTHOR_NAME": "Toolchain Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_NAME": "Toolchain Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.com",
            "GIT_AUTHOR_DATE": "2026-11-07T12:00:00Z",
            "GIT_COMMITTER_DATE": "2026-11-07T12:00:00Z",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "LC_ALL": "C",
            "GIT_PROTOCOL": "version=2",
        }

        # 1. Initialize server repository
        srv = os.path.join(tmp, "srv")
        subprocess.run(["git", "init", "-b", "main", srv], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", srv, "config", "user.name", "Server Admin"], check=True, env=env)
        subprocess.run(["git", "-C", srv, "config", "user.email", "admin@example.com"], check=True, env=env)
        subprocess.run(["git", "-C", srv, "config", "uploadpack.allowFilter", "true"], check=True, env=env)
        subprocess.run(["git", "-C", srv, "config", "uploadpack.allowRefInWant", "true"], check=True, env=env)

        os.makedirs(os.path.join(srv, "src"), exist_ok=True)
        os.makedirs(os.path.join(srv, "docs"), exist_ok=True)
        with open(os.path.join(srv, "README.md"), "w") as f:
            f.write("Project Root\n")
        with open(os.path.join(srv, "src", "main.c"), "w") as f:
            f.write("int main() { return 0; }\n")
        with open(os.path.join(srv, "docs", "manual.txt"), "w") as f:
            f.write("Manual v1\n")
        subprocess.run(["git", "-C", srv, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", srv, "commit", "-m", "Initial commit"], check=True, capture_output=True, env=env)

        env["GIT_AUTHOR_DATE"] = "2026-11-07T12:01:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-11-07T12:01:00Z"
        with open(os.path.join(srv, "src", "main.c"), "w") as f:
            f.write("int main() { return 1; }\n")
        with open(os.path.join(srv, "docs", "manual.txt"), "w") as f:
            f.write("Manual v2\n")
        subprocess.run(["git", "-C", srv, "commit", "-am", "Update code and docs"], check=True, capture_output=True, env=env)

        subprocess.run(["git", "-C", srv, "branch", "experimental"], check=True, env=env)
        subprocess.run(["git", "-C", srv, "tag", "v1.0.0"], check=True, env=env)

        print("=== Step 1: Wire Protocol v2 Capabilities and Negotiation ===")
        proc = subprocess.Popen(["git", "upload-pack", srv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        caps = parse_pkt_stream(proc.stdout)
        print("Server capability advertisement:")
        for t, p in caps:
            if t == "DATA":
                line = p.decode("ascii", errors="replace").strip()
                if line.startswith("agent="):
                    print("  agent=git/<VERSION>")
                else:
                    print(f"  {line}")

        # ls-refs with ref-prefix refs/heads/main
        req1 = (
            make_pkt(b"command=ls-refs\n") +
            b"0001" +
            make_pkt(b"ref-prefix refs/heads/main\n") +
            b"0000"
        )
        proc.stdin.write(req1)
        proc.stdin.flush()
        ls_refs_res = parse_pkt_stream(proc.stdout)
        print("\nls-refs response (filtered by ref-prefix refs/heads/main):")
        for t, p in ls_refs_res:
            if t == "DATA":
                print(f"  {p.decode('ascii').strip()}")

        # fetch with ref-in-want and filter blob:none
        req2 = (
            make_pkt(b"command=fetch\n") +
            make_pkt(b"agent=toolchain-client\n") +
            b"0001" +
            make_pkt(b"want-ref refs/heads/main\n") +
            make_pkt(b"filter blob:none\n") +
            make_pkt(b"done\n") +
            b"0000"
        )
        proc.stdin.write(req2)
        proc.stdin.flush()
        fetch_res = parse_pkt_stream(proc.stdout)
        print("\nfetch response (ref-in-want + filter blob:none):")
        for t, p in fetch_res:
            if t == "DATA":
                if p.startswith(b"\x01PACK"):
                    sig, ver, count = struct.unpack(">4sII", p[1:13])
                    print(f"  [packfile payload]: signature={sig.decode()} version={ver} object_count={count}")
                elif p.startswith(b"\x02") or p.startswith(b"\x03"):
                    pass
                elif len(p) > 1 and p[0] == 1:
                    # Trailing sideband 1 chunk
                    pass
                else:
                    print(f"  {p.decode('ascii', errors='replace').strip()}")
            elif t == "DELIM":
                print("  [delimiter-pkt 0001]")

        proc.stdin.close()
        proc.stdout.close()
        proc.kill()

        # 2. Blobless clone
        print("\n=== Step 2: Blobless Partial Clone and Promisor Remotes ===")
        client = os.path.join(tmp, "client")
        subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none", f"file://{srv}", client], check=True, capture_output=True, env=env)

        pack_dir = os.path.join(client, ".git", "objects", "pack")
        pack_files = sorted(os.listdir(pack_dir))
        promisor_files = [f for f in pack_files if f.endswith(".promisor")]
        print(f"Promisor packfile count (pre-checkout): {len(promisor_files)}")
        for pf in promisor_files:
            stem = pf[:-9]
            has_pack = os.path.exists(os.path.join(pack_dir, stem + ".pack"))
            has_idx = os.path.exists(os.path.join(pack_dir, stem + ".idx"))
            print(f"  companion pack={has_pack}, idx={has_idx}")

        promisor_cfg = subprocess.check_output(["git", "-C", client, "config", "remote.origin.promisor"], env=env, text=True).strip()
        filter_cfg = subprocess.check_output(["git", "-C", client, "config", "remote.origin.partialCloneFilter"], env=env, text=True).strip()
        print(f"Config: remote.origin.promisor={promisor_cfg}, partialCloneFilter={filter_cfg}")

        missing_before = [l[1:] for l in subprocess.check_output(["git", "-C", client, "rev-list", "--objects", "--missing=print", "HEAD"], env=env, text=True).splitlines() if l.startswith("?")]
        print(f"Missing objects in local object store (pre-checkout): {len(missing_before)}")

        # 3. Checkout HEAD
        print("\n=== Step 3: Checkout and Working Tree Blob Faulting ===")
        co_trace = os.path.join(tmp, "checkout.trace")
        subprocess.run(["git", "-C", client, "checkout", "main"], check=True, capture_output=True, env={**env, "GIT_TRACE_PACKET": co_trace})
        print(f"Fetch requests during checkout: {count_trace(co_trace, 'fetch> command=fetch')} (blobs requested: {count_trace(co_trace, 'fetch> want ')})")
        on_disk = sorted(os.path.relpath(os.path.join(d, n), client) for d, ds, fs in os.walk(client) if ".git" not in d.split(os.sep) for n in fs)
        print(f"Working tree files after checkout: {' '.join(on_disk)}")
        promisor_post_co = [f for f in os.listdir(pack_dir) if f.endswith(".promisor")]
        print(f"Promisor packfile count (post-checkout): {len(promisor_post_co)}")

        missing_post_co = [l[1:] for l in subprocess.check_output(["git", "-C", client, "rev-list", "--objects", "--missing=print", "HEAD"], env=env, text=True).splitlines() if l.startswith("?")]
        print(f"Missing objects in local object store (post-checkout): {len(missing_post_co)}")

        # 4. Historical blob backfill
        print("\n=== Step 4: Batched Historical Backfill ===")
        bf_trace = os.path.join(tmp, "backfill.trace")
        subprocess.run(["git", "-C", client, "backfill"], check=True, capture_output=True, env={**env, "GIT_TRACE_PACKET": bf_trace})
        print(f"Fetch requests during backfill: {count_trace(bf_trace, 'fetch> command=fetch')} (blobs requested: {count_trace(bf_trace, 'fetch> want ')})")
        missing_post_bf = [l[1:] for l in subprocess.check_output(["git", "-C", client, "rev-list", "--objects", "--missing=print", "HEAD"], env=env, text=True).splitlines() if l.startswith("?")]
        print(f"Missing objects after git backfill: {len(missing_post_bf)}")

        # 5. Sparse checkout cone mode
        print("\n=== Step 5: Sparse Checkout Cone Mode and Index Flags ===")
        subprocess.run(["git", "-C", client, "sparse-checkout", "set", "--cone", "src"], check=True, capture_output=True, env=env)
        cone_patterns = open(os.path.join(client, ".git", "info", "sparse-checkout")).read().strip().splitlines()
        print("Cone patterns in .git/info/sparse-checkout:")
        for p in cone_patterns:
            print(f"  {p}")

        ls_files = sorted(subprocess.check_output(["git", "-C", client, "ls-files", "-t"], env=env, text=True).strip().splitlines())
        print("Index skip-worktree status flags (ls-files -t):")
        for line in ls_files:
            print(f"  {line}")

        on_disk_sparse = sorted(os.path.relpath(os.path.join(d, n), client) for d, ds, fs in os.walk(client) if ".git" not in d.split(os.sep) for n in fs)
        print(f"Working tree files after sparse-checkout set: {' '.join(on_disk_sparse)}")

        # 6. Worktree isolation
        print("\n=== Step 6: Multi-Worktree Sparse Isolation ===")
        wt_docs = os.path.join(tmp, "wt-docs")
        subprocess.run(["git", "-C", client, "worktree", "add", "-b", "docs-work", wt_docs], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", wt_docs, "sparse-checkout", "set", "--cone", "docs"], check=True, capture_output=True, env=env)

        wt_cfg_status = subprocess.check_output(["git", "-C", client, "config", "extensions.worktreeConfig"], env=env, text=True).strip()
        print(f"extensions.worktreeConfig enabled: {wt_cfg_status}")

        main_cone = open(os.path.join(client, ".git", "info", "sparse-checkout")).read().strip().splitlines()
        wt_cone_file = os.path.join(client, ".git", "worktrees", "wt-docs", "info", "sparse-checkout")
        wt_cone = open(wt_cone_file).read().strip().splitlines()
        print(f"Main worktree cone paths: {' '.join(main_cone)}")
        print(f"Linked worktree cone paths: {' '.join(wt_cone)}")

        # The pattern files are per-worktree regardless of the extension; the extension isolates the config keys.
        shared_cfg = open(os.path.join(client, ".git", "config")).read()
        main_cfg = os.path.join(client, ".git", "config.worktree")
        wt_cfg = os.path.join(client, ".git", "worktrees", "wt-docs", "config.worktree")
        print(f"core.sparseCheckout in shared .git/config: {'sparsecheckout' in shared_cfg.lower()}")
        print(f"core.sparseCheckout in main config.worktree: {os.path.exists(main_cfg) and 'sparseCheckout' in open(main_cfg).read()}")
        print(f"core.sparseCheckout in linked config.worktree: {os.path.exists(wt_cfg) and 'sparseCheckout' in open(wt_cfg).read()}")
        wt_files = sorted(f for f in os.listdir(wt_docs) if f != ".git")
        print(f"Linked worktree directory entries: {' '.join(wt_files)}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
