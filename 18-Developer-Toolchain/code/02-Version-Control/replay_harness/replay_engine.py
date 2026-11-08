#!/usr/bin/env python3
"""Deterministic verification harness for git replay and server-side history rewriting.

Demonstrates worktree-free and index-free tree merging (git merge-tree --write-tree),
atomic history replaying across stacked branches (git replay), and speculative
merge queue build train mechanics in a bare repository.
"""

import os
import re
import shutil
import subprocess
import sys

BARE_LAYOUT = {"HEAD", "branches", "config", "description", "hooks", "info", "objects", "refs", "packed-refs"}
MIN_GIT = (2, 54, 0)  # git replay --revert; --ref-action needs 2.53.0


def die(msg):
    print(f"FAILED CHECK: {msg}", file=sys.stderr)
    sys.exit(3)


def require(cond, msg):
    if not cond:
        die(msg)


def index_artifacts(git_dir):
    """Paths under git_dir that are an index or an index lock."""
    found = []
    for root, _dirs, files in os.walk(git_dir):
        for name in files:
            if name in ("index", "index.lock"):
                found.append(os.path.join(root, name))
    return found


def main():
    if len(sys.argv) < 2:
        print("Usage: replay_engine.py <scratch_dir>")
        sys.exit(1)

    ver = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", ver)
    if m is None or tuple(int(x) for x in m.groups()) < MIN_GIT:
        sys.stderr.write(f"this lab needs Git >= {'.'.join(map(str, MIN_GIT))}, found: {ver.strip()}\n")
        sys.exit(2)

    scratch_dir = sys.argv[1]
    bare_dir = os.path.join(scratch_dir, "server.git")

    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Toolchain Engineer",
        "GIT_AUTHOR_EMAIL": "engineer@example.com",
        "GIT_COMMITTER_NAME": "Forge Merge Queue",
        "GIT_COMMITTER_EMAIL": "queue@forge.internal",
        "GIT_AUTHOR_DATE": "2026-11-08T12:00:00Z",
        "GIT_COMMITTER_DATE": "2026-11-08T12:00:00Z",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "LC_ALL": "C",
        # Any attempt to read or write an index fails: its directory does not exist.
        "GIT_INDEX_FILE": os.path.join(scratch_dir, "no-such-dir", "index"),
    })

    # 1. Initialize bare repository
    subprocess.run(["git", "init", "-b", "main", "--bare", bare_dir],
                   env=env, check=True, capture_output=True, text=True)

    calls = {"n": 0}

    def git(cmd_args, stdin_data=None):
        res = subprocess.run(["git"] + cmd_args, cwd=bare_dir, env=env,
                             capture_output=True, text=True, input=stdin_data)
        calls["n"] += 1
        leaked = index_artifacts(bare_dir)
        require(not leaked, f"index artifact after git {' '.join(cmd_args)}: {leaked}")
        return res.returncode, res.stdout, res.stderr

    def snapshot_refs():
        rc, out, _ = git(["for-each-ref", "--format=%(refname) %(objectname)"])
        require(rc == 0, "for-each-ref failed")
        return set(out.splitlines())

    def make_tree(files_dict):
        dirs = {}
        for path, content in files_dict.items():
            parts = path.split("/", 1)
            if len(parts) == 1:
                dirs.setdefault("", {})[parts[0]] = content
            else:
                dirs.setdefault(parts[0], {})[parts[1]] = content

        subtrees = {}
        for d, files in sorted(dirs.items()):
            if d == "":
                continue
            sub_lines = []
            for fname, content in sorted(files.items()):
                rc, b_oid, _ = git(["hash-object", "-w", "--stdin"], stdin_data=content)
                sub_lines.append(f"100644 blob {b_oid.strip()}\t{fname}\n")
            rc, t_oid, _ = git(["mktree"], stdin_data="".join(sub_lines))
            subtrees[d] = t_oid.strip()

        root_lines = []
        for fname, content in sorted(dirs.get("", {}).items()):
            rc, b_oid, _ = git(["hash-object", "-w", "--stdin"], stdin_data=content)
            root_lines.append(f"100644 blob {b_oid.strip()}\t{fname}\n")
        for dname, t_oid in sorted(subtrees.items()):
            root_lines.append(f"040000 tree {t_oid}\t{dname}\n")
        rc, root_oid, _ = git(["mktree"], stdin_data="".join(root_lines))
        return root_oid.strip()

    def make_commit(files_dict, message, parents=()):
        t_oid = make_tree(files_dict)
        parent_args = []
        for p in parents:
            parent_args.extend(["-p", p])
        rc, c_oid, _ = git(["commit-tree", t_oid] + parent_args + ["-m", message])
        return c_oid.strip(), t_oid

    # Construct initial repository commit graph
    c_base, t_base = make_commit({
        "src/core.c": "int core_init() { return 0; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "README.md": "# Project Baseline\nInitial baseline documentation.\n"
    }, "Base commit")
    git(["update-ref", "refs/heads/main", c_base])

    # Main advances: edits README.md
    c_main, t_main = make_commit({
        "src/core.c": "int core_init() { return 0; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "README.md": "# Project Baseline\nUpdated baseline documentation on main.\n"
    }, "Update README on main", parents=[c_base])
    git(["update-ref", "refs/heads/main", c_main])

    # feat-A: edits src/core.c (clean merge with main)
    c_featA, t_featA = make_commit({
        "src/core.c": "int core_init() { /* fast-path */ return 1; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "README.md": "# Project Baseline\nInitial baseline documentation.\n"
    }, "Optimize core_init", parents=[c_base])
    git(["update-ref", "refs/heads/feat-A", c_featA])

    # feat-B: stacked on feat-A, edits src/util.c
    c_featB, t_featB = make_commit({
        "src/core.c": "int core_init() { /* fast-path */ return 1; }\n",
        "src/util.c": "int util_calc() { /* simd */ return 84; }\n",
        "README.md": "# Project Baseline\nInitial baseline documentation.\n"
    }, "Vectorize util_calc", parents=[c_featA])
    git(["update-ref", "refs/heads/feat-B", c_featB])

    # feat-conflict: edits README.md differently from main
    c_conflict, t_conflict = make_commit({
        "src/core.c": "int core_init() { return 0; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "README.md": "# Project Baseline\nConflicting documentation rewrite.\n"
    }, "Conflicting README rewrite", parents=[c_base])
    git(["update-ref", "refs/heads/feat-conflict", c_conflict])

    # feat-C: forked from the base, independent of main and of the stack; used for --advance
    c_featC, t_featC = make_commit({
        "src/core.c": "int core_init() { return 0; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "src/extra.c": "int extra_hook() { return 7; }\n",
        "README.md": "# Project Baseline\nInitial baseline documentation.\n"
    }, "Add extra_hook", parents=[c_base])
    git(["update-ref", "refs/heads/feat-C", c_featC])

    print("=== Worktree-Free & Index-Free Verification ===")
    rc, bare_val, _ = git(["config", "--get", "core.bare"])
    print(f"Repository mode: bare (core.bare = {bare_val.strip()})")
    rc, is_bare, _ = git(["rev-parse", "--is-bare-repository"])
    rc, in_wt, _ = git(["rev-parse", "--is-inside-work-tree"])
    rc_top, _, _ = git(["rev-parse", "--show-toplevel"])
    require(is_bare.strip() == "true", "repository is not bare")
    require(in_wt.strip() == "false" and rc_top != 0, "a working tree is reported")
    print(f"git rev-parse --is-inside-work-tree: {in_wt.strip()}, --show-toplevel exit code is nonzero: {rc_top != 0}")
    print(f"Index artifacts in server.git: {len(index_artifacts(bare_dir))}")

    # Positive control: the artifact scan must see an index when git really creates one.
    control_dir = os.path.join(scratch_dir, "control.git")
    shutil.copytree(bare_dir, control_dir)
    control_env = {k: v for k, v in env.items() if k != "GIT_INDEX_FILE"}
    subprocess.run(["git", "read-tree", "main"], cwd=control_dir, env=control_env,
                   check=True, capture_output=True, text=True)
    require(len(index_artifacts(control_dir)) == 1, "positive control did not produce an index")
    print(f"Positive control (git read-tree in a copy): index artifacts seen: {len(index_artifacts(control_dir))}")
    shutil.rmtree(control_dir)

    print("\n=== 1. Plumbing Three-Way Tree Merging (git merge-tree) ===")
    # Clean merge
    rc, out_clean, _ = git(["merge-tree", "--write-tree", "main", "feat-A"])
    require(rc == 0, f"clean merge-tree exit code {rc}")
    clean_tree = out_clean.strip()
    print(f"Clean merge exit code: {rc}")
    print(f"Clean merged tree OID: {clean_tree}")
    print("Clean tree entries:")
    rc, ls_tree, _ = git(["ls-tree", clean_tree])
    for line in ls_tree.strip().splitlines():
        parts = line.split()
        mode, obj_type, oid, path = parts[0], parts[1], parts[2], parts[3]
        print(f"  {mode} {obj_type} {path} ({oid})")
    # The merged tree must hold main's README and feat-A's core.c.
    rc, readme_clean, _ = git(["cat-file", "-p", f"{clean_tree}:README.md"])
    rc, core_clean, _ = git(["cat-file", "-p", f"{clean_tree}:src/core.c"])
    require("Updated baseline documentation on main" in readme_clean, "clean merge lost main's README edit")
    require("fast-path" in core_clean, "clean merge lost feat-A's core.c edit")

    # Conflicted merge
    rc, out_conf, _ = git(["merge-tree", "--write-tree", "main", "feat-conflict"])
    require(rc == 1, f"conflicted merge-tree exit code {rc}")
    print(f"Conflicted merge exit code: {rc}")
    conf_lines = out_conf.splitlines()
    conf_tree = conf_lines[0].strip()
    print(f"Conflicted merged tree OID: {conf_tree}")
    print("Conflicted stage entries:")
    stages_seen = []
    for line in conf_lines[1:]:
        if "\t" in line and len(line.split()) >= 3:
            parts = line.split()
            mode = parts[0]
            oid = parts[1]
            stage = parts[2]
            path = line.split("\t", 1)[1]
            stage_name = {
                "1": "base",
                "2": "ours",
                "3": "theirs"
            }.get(stage, f"stage {stage}")
            stages_seen.append(stage)
            print(f"  stage {stage} ({stage_name}): {oid} {path}")
    require(stages_seen == ["1", "2", "3"], f"stage entries {stages_seen}")

    # Inspect conflicted blob markers
    rc, blob_content, _ = git(["cat-file", "-p", f"{conf_tree}:README.md"])
    print("Conflicted blob conflict markers:")
    markers = [b for b in blob_content.splitlines()
               if any(marker in b for marker in ["<<<<<<<", "=======", ">>>>>>>"])]
    require(len(markers) == 3, f"expected 3 conflict marker lines, saw {markers}")
    for bline in markers:
        print(f"  {bline}")

    # Batch merge streaming over stdin
    rc, out_stdin, _ = git(["merge-tree", "--write-tree", "--stdin"],
                           stdin_data="main feat-A\nmain feat-conflict\n")
    require(rc == 0, f"merge-tree --stdin exit code {rc}")
    # Parse NUL-separated stdin output
    tokens = out_stdin.split("\0")
    print("Stdin batch merge stream (2 requests):")
    # Request 1: clean (1, tree, empty)
    r1_status = tokens[0]
    r1_tree = tokens[1]
    print(f"  request 1 status: {r1_status} (clean), tree: {r1_tree}")
    # Request 2: conflict (0, tree, stages..., empty, messages..., empty)
    # Token 2 is empty separator, Token 3 is status '0', Token 4 is tree
    r2_status = tokens[3]
    r2_tree = tokens[4]
    print(f"  request 2 status: {r2_status} (conflict), tree: {r2_tree}")
    require((r1_status, r1_tree) == ("1", clean_tree), "stdin request 1 differs from the one-shot merge")
    require((r2_status, r2_tree) == ("0", conf_tree), "stdin request 2 differs from the one-shot merge")

    print("\n=== 2. Server-Side History Rewriting (git replay) ===")
    # Replay print mode: must emit a command and change no ref
    refs_before = snapshot_refs()
    rc, out_print, _ = git(["replay", "--ref-action=print", "--onto=main", f"{c_base}..feat-A"])
    require(rc == 0, f"replay --ref-action=print exit code {rc}")
    require(snapshot_refs() == refs_before, "--ref-action=print modified a ref")
    print("Single branch replay preview (--ref-action=print):")
    print(f"  {out_print.strip()}")
    print(f"  refs modified by the preview: {len(snapshot_refs() ^ refs_before)}")

    # Atomicity: a held lock on one ref of the set must abort the whole --contained update.
    lock_path = os.path.join(bare_dir, "refs", "heads", "feat-B.lock")
    with open(lock_path, "w") as fh:
        fh.write("")
    refs_before = snapshot_refs()
    rc_locked, _, _ = git(["replay", "--contained", "--onto=main", f"{c_base}..feat-B"])
    refs_after = snapshot_refs()
    if os.path.exists(lock_path):
        os.remove(lock_path)
    require(rc_locked != 0, "replay succeeded although refs/heads/feat-B.lock was held")
    require(refs_after == refs_before, "a partial ref update survived the locked transaction")
    print("Atomicity probe (--contained with refs/heads/feat-B.lock held):")
    print(f"  replay failed: {rc_locked != 0}, refs modified: {len(refs_after ^ refs_before)}")

    # Contained stacked branches rebase
    rc, _, _ = git(["replay", "--contained", "--onto=main", f"{c_base}..feat-B"])
    require(rc == 0, f"replay --contained exit code {rc}")
    print("Stacked branches atomic replay (--contained --onto=main):")
    print(f"  exit code: {rc}")
    rc, featA_tip, _ = git(["rev-parse", "refs/heads/feat-A"])
    rc, featA_parent, _ = git(["rev-parse", "refs/heads/feat-A^"])
    rc, featB_tip, _ = git(["rev-parse", "refs/heads/feat-B"])
    rc, featB_parent, _ = git(["rev-parse", "refs/heads/feat-B^"])
    require(featA_parent.strip() == c_main, "feat-A is not on top of main")
    require(featB_parent.strip() == featA_tip.strip(), "feat-B is not on top of the new feat-A")
    require(featA_tip.strip() != c_featA and featB_tip.strip() != c_featB, "stack was not rewritten")
    print(f"  refs/heads/feat-A: {featA_tip.strip()} (parent: {featA_parent.strip()})")
    print(f"  refs/heads/feat-B: {featB_tip.strip()} (parent: {featB_parent.strip()})")

    # Advance mode: feat-C was never rewritten, so a real commit must be synthesized.
    git(["branch", "advance-branch", c_main])
    rc_adv, _, _ = git(["replay", "--advance=advance-branch", f"{c_base}..feat-C"])
    require(rc_adv == 0, f"replay --advance exit code {rc_adv}")
    rc, adv_tip, _ = git(["rev-parse", "advance-branch"])
    rc, adv_parent, _ = git(["rev-parse", "advance-branch^"])
    rc, adv_tree, _ = git(["rev-parse", "advance-branch^{tree}"])
    rc, expect_out, _ = git(["merge-tree", "--write-tree", "main", "feat-C"])
    rc, adv_msg, _ = git(["log", "-1", "--format=%s", "advance-branch"])
    require(adv_tip.strip() != c_featC, "advance reused the original commit")
    require(adv_parent.strip() == c_main, "advance did not build on the old branch tip")
    require(adv_tree.strip() == expect_out.strip(), "advance tree differs from the merge-tree result")
    require(adv_msg.strip() == "Add extra_hook", "advance lost the commit message")
    print("Branch advance replay (--advance=advance-branch):")
    print(f"  git replay exit code: {rc_adv}")
    print(f"  refs/heads/advance-branch: {adv_tip.strip()} (parent: {adv_parent.strip()})")
    print(f"  new commit synthesized: {adv_tip.strip() != c_featC}")
    print(f"  tree equals merge-tree result: {adv_tree.strip() == expect_out.strip()}")

    # Revert mode: reverting the only change on top of main must restore main's tree.
    git(["branch", "revert-branch", adv_tip.strip()])
    rc_rev, _, _ = git(["replay", "--revert=revert-branch", f"{c_main}..advance-branch"])
    require(rc_rev == 0, f"replay --revert exit code {rc_rev}")
    rc, rev_tip, _ = git(["rev-parse", "revert-branch"])
    rc, rev_parent, _ = git(["rev-parse", "revert-branch^"])
    rc, rev_tree, _ = git(["rev-parse", "revert-branch^{tree}"])
    rc, main_tree, _ = git(["rev-parse", "main^{tree}"])
    rc, rev_msg, _ = git(["log", "-1", "--format=%s", rev_tip.strip()])
    require(rev_parent.strip() == adv_tip.strip(), "revert commit is not on top of the old tip")
    require(rev_tree.strip() == main_tree.strip(), "revert did not restore main's tree")
    require(rev_msg.strip() == 'Revert "Add extra_hook"', f"revert message {rev_msg.strip()!r}")
    print("Server-side revert replay (--revert=revert-branch):")
    print(f"  git replay exit code: {rc_rev}")
    print(f"  revert commit: {rev_tip.strip()}")
    print(f"  commit message: {rev_msg.strip()}")
    print(f"  tree equals main's tree: {rev_tree.strip() == main_tree.strip()}")

    # Conflict abort behavior
    refs_before = snapshot_refs()
    rc, out_conf_rep, err_conf_rep = git(["replay", "--onto=main", f"{c_base}..feat-conflict"])
    refs_modified = len(snapshot_refs() ^ refs_before)
    require(rc == 1, f"conflicting replay exit code {rc}")
    require(refs_modified == 0, "a conflicting replay modified a ref")
    print("Conflict abort behavior:")
    print(f"  exit code: {rc} (stdout len: {len(out_conf_rep)}, stderr len: {len(err_conf_rep)}, refs modified: {refs_modified})")

    print("\n=== 3. High-Throughput Speculative Merge Queue Simulation ===")
    # Construct 3 pull requests branched from c_base
    c_pr1, _ = make_commit({
        "src/core.c": "int core_init() { return 0; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "src/auth.c": "int auth_verify() { return 1; }\n",
        "README.md": "# Project Baseline\nInitial baseline documentation.\n"
    }, "PR 1: authentication module", parents=[c_base])
    git(["update-ref", "refs/heads/pr-1", c_pr1])

    c_pr2, _ = make_commit({
        "src/core.c": "int core_init() { return 0; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "src/billing.c": "int billing_process() { return 100; }\n",
        "README.md": "# Project Baseline\nInitial baseline documentation.\n"
    }, "PR 2: payment billing engine", parents=[c_base])
    git(["update-ref", "refs/heads/pr-2", c_pr2])

    c_pr3, _ = make_commit({
        "src/core.c": "int core_init() { return 0; }\n",
        "src/util.c": "int util_calc() { return 42; }\n",
        "src/metrics.c": "void metrics_record() {}\n",
        "README.md": "# Project Baseline\nInitial baseline documentation.\n"
    }, "PR 3: telemetry metrics", parents=[c_base])
    git(["update-ref", "refs/heads/pr-3", c_pr3])

    def replay_ok(args):
        rc, _, err = git(["replay"] + args)
        require(rc == 0, f"git replay {' '.join(args)} exit code {rc}: {err.strip()}")

    def tree_files(rev):
        rc, out, _ = git(["ls-tree", "-r", "--name-only", rev])
        return set(out.split())

    # Assemble speculative train
    # Car 1: main + PR1
    git(["branch", "train/car-1", "main"])
    replay_ok(["--advance=train/car-1", f"{c_base}..pr-1"])
    car1_tip = git(["rev-parse", "train/car-1"])[1].strip()
    car1_tree = git(["rev-parse", "train/car-1^{tree}"])[1].strip()

    # Car 2: Car 1 + PR2
    git(["branch", "train/car-2", "train/car-1"])
    replay_ok(["--advance=train/car-2", f"{c_base}..pr-2"])
    car2_tip = git(["rev-parse", "train/car-2"])[1].strip()
    car2_tree = git(["rev-parse", "train/car-2^{tree}"])[1].strip()

    # Car 3: Car 2 + PR3
    git(["branch", "train/car-3", "train/car-2"])
    replay_ok(["--advance=train/car-3", f"{c_base}..pr-3"])
    car3_tip = git(["rev-parse", "train/car-3"])[1].strip()
    car3_tree = git(["rev-parse", "train/car-3^{tree}"])[1].strip()

    require(git(["rev-parse", "train/car-1^"])[1].strip() == c_main, "car 1 is not on main")
    require(git(["rev-parse", "train/car-2^"])[1].strip() == car1_tip, "car 2 is not on car 1")
    require(git(["rev-parse", "train/car-3^"])[1].strip() == car2_tip, "car 3 is not on car 2")
    require({"src/auth.c", "src/billing.c", "src/metrics.c"} <= tree_files("train/car-3"),
            "car 3 does not carry all three PRs")

    print("Speculative Build Train:")
    print(f"  Car 1 (main + PR-1): tip={car1_tip[:7]} tree={car1_tree[:7]}")
    print(f"  Car 2 (Car-1 + PR-2): tip={car2_tip[:7]} tree={car2_tree[:7]}")
    print(f"  Car 3 (Car-2 + PR-3): tip={car3_tip[:7]} tree={car3_tree[:7]}")

    print("Simulating CI test failure on PR-1:")
    # Evacuate train and re-speculate Car 2 and Car 3 directly on main
    git(["branch", "-D", "train/car-1", "train/car-2", "train/car-3"])

    git(["branch", "train/car-2", "main"])
    replay_ok(["--advance=train/car-2", f"{c_base}..pr-2"])
    respec_car2_tip = git(["rev-parse", "train/car-2"])[1].strip()
    respec_car2_tree = git(["rev-parse", "train/car-2^{tree}"])[1].strip()

    git(["branch", "train/car-3", "train/car-2"])
    replay_ok(["--advance=train/car-3", f"{c_base}..pr-3"])
    respec_car3_tip = git(["rev-parse", "train/car-3"])[1].strip()
    respec_car3_tree = git(["rev-parse", "train/car-3^{tree}"])[1].strip()

    require(git(["rev-parse", "train/car-2^"])[1].strip() == c_main, "new car 2 is not on main")
    require(git(["rev-parse", "train/car-3^"])[1].strip() == respec_car2_tip, "new car 3 is not on new car 2")
    new_files = tree_files("train/car-3")
    require("src/auth.c" not in new_files and {"src/billing.c", "src/metrics.c"} <= new_files,
            "evicted PR-1 still present, or PR-2/PR-3 missing")

    print("Evacuation and rapid re-speculation:")
    print(f"  New Car 2 (main + PR-2): tip={respec_car2_tip[:7]} tree={respec_car2_tree[:7]}")
    print(f"  New Car 3 (New Car-2 + PR-3): tip={respec_car3_tip[:7]} tree={respec_car3_tree[:7]}")

    print("Ancestry of New Car 3:")
    rc, respec_log, _ = git(["log", "--graph", "--oneline", "train/car-3"])
    for l in respec_log.strip().splitlines():
        print(f"  {l}")

    print("\n=== Integrity and Lock Invariants ===")
    leaked = index_artifacts(bare_dir)
    stray = sorted(set(os.listdir(bare_dir)) - BARE_LAYOUT)
    require(not leaked, f"index artifacts at the end: {leaked}")
    require(not stray, f"files outside the bare repository layout: {stray}")
    print(f"Git commands run, each followed by an index scan: {calls['n']}")
    print(f"Index artifacts in server.git at the end: {len(leaked)}")
    print(f"Entries outside the bare layout (a checkout would add some): {len(stray)}")

if __name__ == "__main__":
    main()
