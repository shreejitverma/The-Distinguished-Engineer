#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile

FAILURES = []


def check(label, ok):
    """Print a MATCH/FAIL line and remember failures so the script can exit non-zero."""
    if not ok:
        FAILURES.append(label)
    return "MATCH" if ok else "FAIL"


def git_out(env, *args):
    return subprocess.run(["git", *args], env=env, capture_output=True, text=True).stdout.strip()


def commit_graph_chunk_ids(path):
    """Return the chunk ids of a commit-graph file (header is 8 bytes, then a lookup table)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"CGPH":
        return []
    count = data[6]
    ids = []
    for i in range(count):
        off = 8 + i * 12
        ids.append(data[off:off + 4].decode("ascii"))
    return ids


def main():
    with tempfile.TemporaryDirectory() as td:
        home = os.path.join(td, "home")
        os.makedirs(home)
        global_cfg = os.path.join(td, "global_gitconfig")
        open(global_cfg, "w").close()
        
        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = global_cfg
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        env["GIT_AUTHOR_NAME"] = "Enlistment Author"
        env["GIT_AUTHOR_EMAIL"] = "author@example.com"
        env["GIT_COMMITTER_NAME"] = "Enlistment Committer"
        env["GIT_COMMITTER_EMAIL"] = "committer@example.com"
        env["GIT_AUTHOR_DATE"] = "2026-11-08T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-11-08T00:00:00Z"
        env["LC_ALL"] = "C"
        # `scalar clone` runs `git maintenance start`; keep the schedule files inside the tempdir
        # (~ resolves through HOME) and replace every scheduler binary with `true`.
        env["HOME"] = home
        env["GIT_TEST_MAINT_SCHEDULER"] = "launchctl:true,systemctl:true,crontab:true,schtasks:true"

        # 1. Initialize Upstream Bare Repository
        upstream = os.path.join(td, "upstream.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", upstream], check=True, env=env, capture_output=True)
        # A file:// upstream ignores --filter unless it allows filtering, so the clone would not be blobless.
        subprocess.run(["git", "-C", upstream, "config", "uploadpack.allowFilter", "true"], check=True, env=env)

        # 2. Populate Seed Monorepo with Multi-Package Layout
        seed = os.path.join(td, "seed")
        subprocess.run(["git", "clone", upstream, seed], check=True, env=env, capture_output=True)
        os.makedirs(os.path.join(seed, "packages", "engine"), exist_ok=True)
        os.makedirs(os.path.join(seed, "packages", "ui"), exist_ok=True)
        os.makedirs(os.path.join(seed, "packages", "docs"), exist_ok=True)
        os.makedirs(os.path.join(seed, "tools", "benchmarks"), exist_ok=True)

        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("# Enterprise Monorepo\n")
        with open(os.path.join(seed, "Makefile"), "w") as f:
            f.write("all:\n\t@echo ok\n")
        with open(os.path.join(seed, "packages", "engine", "engine.c"), "w") as f:
            f.write("int engine_init(void) { return 0; }\n")
        with open(os.path.join(seed, "packages", "engine", "engine.h"), "w") as f:
            f.write("#pragma once\nint engine_init(void);\n")
        with open(os.path.join(seed, "packages", "ui", "view.c"), "w") as f:
            f.write("int ui_render(void) { return 0; }\n")
        with open(os.path.join(seed, "packages", "ui", "view.h"), "w") as f:
            f.write("#pragma once\nint ui_render(void);\n")
        with open(os.path.join(seed, "packages", "docs", "architecture.md"), "w") as f:
            f.write("# Architecture\n")
        with open(os.path.join(seed, "packages", "docs", "api.md"), "w") as f:
            f.write("# API\n")
        with open(os.path.join(seed, "tools", "benchmarks", "bench.c"), "w") as f:
            f.write("int bench(void) { return 0; }\n")

        subprocess.run(["git", "-C", seed, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", seed, "commit", "-m", "feat: initial monorepo structure"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", seed, "push", "origin", "main"], check=True, env=env, capture_output=True)

        # 3. Execute Scalar Clone to establish enlistment
        enlistment = os.path.join(td, "enlistment")
        subprocess.run(["scalar", "clone", f"file://{upstream}", enlistment], check=True, env=env, capture_output=True)
        src = os.path.join(enlistment, "src")

        print("=== 1. Enlistment Layout ===")
        print("Enlistment root entries:", sorted(os.listdir(enlistment)))
        print("Worktree populated root files:", sorted(e for e in os.listdir(src) if not e.startswith(".")))
        populated_subtrees = [d for d in ["packages", "tools"] if os.path.exists(os.path.join(src, d))]
        print("Subtrees materialized on disk initially:", populated_subtrees)
        missing = [l for l in git_out(env, "-C", src, "rev-list", "--objects", "--all", "--missing=print").splitlines() if l.startswith("?")]
        print(f"Blobs absent after clone (blobless): {len(missing)} [{check('blobless', len(missing) > 0)}]")

        print("\n=== 2. Config Audit ===")
        checked_keys = [
            ("core.fsmonitor", "true"),
            ("core.untrackedCache", "true"),
            ("index.version", "4"),
            ("index.skipHash", "true"),
            ("maintenance.strategy", "incremental"),
            ("gc.auto", "0"),
            ("status.aheadBehind", "false"),
            ("fetch.unpackLimit", "1"),
            ("fetch.showForcedUpdates", "false"),
            ("pack.useBitmaps", "false"),
            ("pack.usePathWalk", "true"),
            ("remote.origin.promisor", "true"),
            ("remote.origin.partialclonefilter", "blob:none"),
        ]
        for key, expected in checked_keys:
            val = subprocess.run(["git", "-C", src, "config", key], env=env, capture_output=True, text=True).stdout.strip()
            print(f"  {key} = {val} [{check(key, val == expected)}]")

        wt_sparse = subprocess.run(["git", "-C", src, "config", "--worktree", "core.sparseCheckoutCone"], env=env, capture_output=True, text=True).stdout.strip()
        print(f"  core.sparseCheckoutCone (worktree) = {wt_sparse} [{check('sparseCheckoutCone', wt_sparse == 'true')}]")

        print("\n=== 3. Sparse-Checkout Cone Expansion ===")
        subprocess.run(["git", "-C", src, "sparse-checkout", "set", "packages/engine"], env=env, check=True)
        populated = []
        for root, dirs, files in os.walk(src):
            if ".git" in root:
                continue
            rel = os.path.relpath(root, src)
            if rel != ".":
                for f in sorted(files):
                    populated.append(os.path.join(rel, f))
        print("Populated files after cone expansion:")
        for p in sorted(populated):
            print(f"  {p}")

        print("\n=== 4. Sparse Index Representation ===")
        idx_before = subprocess.run(["git", "-C", src, "config", "--get", "index.sparse"], env=env, capture_output=True, text=True)
        print(f"index.sparse after scalar clone: {'unset' if idx_before.returncode == 1 else idx_before.stdout.strip()} [{check('index.sparse unset', idx_before.returncode == 1)}]")
        full_entries = git_out(env, "-C", src, "ls-files").splitlines()
        print(f"Entries before enabling the sparse index: {len(full_entries)}")
        subprocess.run(["git", "-C", src, "sparse-checkout", "init", "--cone", "--sparse-index"], env=env, check=True)
        res_sparse = subprocess.run(["git", "-C", src, "ls-files", "--sparse"], env=env, capture_output=True, text=True)
        sparse_entries = [line.strip() for line in res_sparse.stdout.splitlines() if line.strip()]
        print("Sparse index entries:")
        for e in sparse_entries:
            print(f"  {e}")
        dir_modes = [l.split()[0] for l in git_out(env, "-C", src, "ls-files", "--sparse", "-s").splitlines() if l.split()[-1].endswith("/")]
        print(f"Directory boundary entries with mode 040000: {dir_modes.count('040000')} of {len(dir_modes)} [{check('040000', len(dir_modes) == 3 and set(dir_modes) == {'040000'})}]")

        print("\n=== 5. Background Maintenance Task ===")
        cg_pre = os.path.join(src, ".git", "objects", "info")
        pre_exists = os.path.exists(os.path.join(cg_pre, "commit-graph")) or os.path.isdir(os.path.join(cg_pre, "commit-graphs"))
        print(f"Commit-graph before the task: {pre_exists}")
        subprocess.run(["scalar", "run", "commit-graph", enlistment], env=env, check=True)
        cg_file = os.path.join(src, ".git", "objects", "info", "commit-graph")
        cg_dir = os.path.join(src, ".git", "objects", "info", "commit-graphs")
        cg_files = ([cg_file] if os.path.isfile(cg_file) else []) + (
            [os.path.join(cg_dir, n) for n in sorted(os.listdir(cg_dir)) if n.endswith(".graph")] if os.path.isdir(cg_dir) else [])
        chunks = sorted({c for f in cg_files for c in commit_graph_chunk_ids(f)})
        print(f"Commit-graph written: {bool(cg_files)} [{check('commit-graph', bool(cg_files))}]")
        print(f"Changed-path Bloom chunks present (BDAT, BIDX): {'BDAT' in chunks and 'BIDX' in chunks} [{check('bloom', 'BDAT' in chunks and 'BIDX' in chunks)}]")

        print("\n=== 6. Enlistment Lifecycle ===")
        reg_list = subprocess.run(["scalar", "list"], env=env, capture_output=True, text=True).stdout.strip()
        is_registered = os.path.realpath(src) in [os.path.realpath(l) for l in reg_list.splitlines() if l.strip()]
        print(f"Registered in scalar.repo: {is_registered} [{check('registered', is_registered)}]")

        # Stop daemon before unregistering
        subprocess.run(["git", "-C", src, "fsmonitor--daemon", "stop"], env=env, capture_output=True)

        res_unreg = subprocess.run(["scalar", "unregister", enlistment], env=env, check=True, capture_output=True, text=True)
        print(f"Unregister executed cleanly: {res_unreg.returncode == 0}")
        after_list = subprocess.run(["scalar", "list"], env=env, capture_output=True, text=True).stdout.strip()
        print(f"Registered after unregister: {len(after_list) > 0} [{check('unregistered', len(after_list) == 0)}]")

    if FAILURES:
        print("FAILED CHECKS: " + ", ".join(FAILURES), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
