#!/usr/bin/env python3
"""Deterministic verification of Jujutsu first-class conflicts and operation log."""

import os
import re
import shutil
import subprocess
import sys
import tempfile


def run_cmd(args, cwd, env, check=True):
    res = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )
    return res


def scrub_ids(text):
    # Scrub the 40-char commit ids that `jj op log` descriptions embed
    text = re.sub(r"\b[0-9a-f]{40}\b", "<oid>", text)
    # Scrub the change id (8 letters k-z) and 8-hex commit id jj puts in conflict marker labels
    text = re.sub(r"\b[k-z]{8} [0-9a-f]{8} (?=\")", "<change> <commit> ", text)
    return to_ascii(text)


def emit(text):
    print(scrub_ids(text))


def to_ascii(text):
    # Map jj graph and tree unicode characters to strict ASCII
    replacements = {
        "\u2502": "|",  # box drawings light vertical
        "\u25cb": "o",  # white circle
        "\u251c": "|",  # box drawings light vertical and right
        "\u2500": "-",  # box drawings light horizontal
        "\u256f": "/",  # box drawings light up and left
        "\u25c6": "*",  # black diamond
        "\u00d7": "x",  # multiplication sign
        "\u25b2": "^",  # black up-pointing triangle
        "\u25bc": "v",  # black down-pointing triangle
    }
    for u, a in replacements.items():
        text = text.replace(u, a)
    return text


MIN_JJ = (0, 33, 0)  # sequential `jj undo` (CHANGELOG 0.33.0) is what Step 10 relies on


def preflight():
    for tool in ("jj", "git"):
        if shutil.which(tool) is None:
            sys.exit(f"jj_conflict_demo needs `{tool}` on PATH (the harness has no requirement for it)")
    out = subprocess.run(["jj", "--version"], stdout=subprocess.PIPE, text=True, check=True).stdout
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if m is None or tuple(int(g) for g in m.groups()) < MIN_JJ:
        sys.exit(f"jj >= {'.'.join(map(str, MIN_JJ))} required (got {out.strip()!r}): Step 10 needs sequential undo")


def git_rebase_conflict(workdir):
    """Run the same two-branch conflict through real Git; return (exit code, rebase left in progress)."""
    env = os.environ.copy()
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="Distinguished Engineer",
        GIT_AUTHOR_EMAIL="engineer@example.com",
        GIT_COMMITTER_NAME="Distinguished Engineer",
        GIT_COMMITTER_EMAIL="engineer@example.com",
        LC_ALL="C",
    )
    cfg = os.path.join(workdir, "service.cfg")

    def git(*args, check=True):
        return run_cmd(["git", *args], cwd=workdir, env=env, check=check)

    def write(workers):
        with open(cfg, "w") as f:
            f.write(f"port = 8080\nworkers = {workers}\ntimeout = 30\n")

    git("init", "-q", "-b", "base")
    write(4)
    git("add", "service.cfg")
    git("commit", "-q", "-m", "base config")
    git("checkout", "-q", "-b", "feature-a")
    write(8)
    git("commit", "-q", "-am", "feature A")
    git("checkout", "-q", "-b", "feature-b", "base")
    write(16)
    git("commit", "-q", "-am", "feature B")
    res = git("rebase", "feature-a", check=False)
    in_progress = os.path.isdir(os.path.join(workdir, ".git", "rebase-merge"))
    return res.returncode, in_progress


def main():
    preflight()
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <repo-dir>")
        sys.exit(1)

    repo_dir = sys.argv[1]
    env = os.environ.copy()
    env["JJ_USER"] = "Distinguished Engineer"
    env["JJ_EMAIL"] = "engineer@example.com"
    env["JJ_CONFIG"] = ""
    env["LC_ALL"] = "C"

    # 1. Initialize repo
    run_cmd(["jj", "git", "init", repo_dir, "--colocate"], cwd=None, env=env)

    # 2. Base commit
    with open(os.path.join(repo_dir, "service.cfg"), "w") as f:
        f.write("port = 8080\nworkers = 4\ntimeout = 30\n")
    run_cmd(["jj", "commit", "-m", "base config"], cwd=repo_dir, env=env)

    # 3. Branch A: workers = 8
    run_cmd(["jj", "bookmark", "create", "feature-a"], cwd=repo_dir, env=env)
    with open(os.path.join(repo_dir, "service.cfg"), "w") as f:
        f.write("port = 8080\nworkers = 8\ntimeout = 30\n")
    run_cmd(["jj", "commit", "-m", "feature A: workers=8"], cwd=repo_dir, env=env)

    # 4. Branch B: workers = 16 (branched from base)
    run_cmd(
        ["jj", "new", "feature-a-", "-m", "feature B: workers=16"],
        cwd=repo_dir,
        env=env,
    )
    run_cmd(["jj", "bookmark", "create", "feature-b"], cwd=repo_dir, env=env)
    with open(os.path.join(repo_dir, "service.cfg"), "w") as f:
        f.write("port = 8080\nworkers = 16\ntimeout = 30\n")

    print("=== Step 1: Initial Divergent Commit Graph ===")
    log_fmt = (
        'separate(" ", if(empty, "(empty)"), if(conflict, "(conflict)"),'
        ' bookmarks, description.first_line()) ++ "\n"'
    )
    res = run_cmd(["jj", "log", "-T", log_fmt], cwd=repo_dir, env=env)
    emit(res.stdout.strip())

    print("\n=== Step 2: Non-Blocking Rebase (feature-b onto feature-a) ===")
    rebase_res = run_cmd(
        ["jj", "rebase", "-b", "feature-b", "-d", "feature-a"],
        cwd=repo_dir,
        env=env,
        check=False,
    )
    print(f"jj rebase exit code: {rebase_res.returncode} (conflict recorded in the commit, rebase completed)")
    with tempfile.TemporaryDirectory() as git_dir:
        git_code, git_stopped = git_rebase_conflict(git_dir)
    print(f"git rebase exit code: {git_code} (rebase left in progress: {git_stopped})")

    print("\n=== Step 3: First-Class Conflict Data Structure in Commit Tree ===")
    tree_fmt = (
        'path ++ ": conflict=" ++ self.conflict() ++ ", sides=" ++'
        " self.conflict_side_count() ++ \", removed=\" ++"
        ' self.removed_values().len() ++ ", added=" ++'
        ' self.added_values().len() ++ "\n"'
    )
    res = run_cmd(["jj", "file", "list", "-T", tree_fmt], cwd=repo_dir, env=env)
    emit(res.stdout.strip())

    print("\n=== Step 4: Commit Graph with Inlined Conflict State ===")
    res = run_cmd(["jj", "log", "-T", log_fmt], cwd=repo_dir, env=env)
    emit(res.stdout.strip())

    print("\n=== Step 5: Working Copy Conflict Markers ===")
    with open(os.path.join(repo_dir, "service.cfg")) as f:
        lines = f.readlines()
    for line in lines:
        emit(line.rstrip("\n"))

    print("\n=== Step 6: Non-Blocking Child Commit on Conflicted Parent ===")
    run_cmd(
        ["jj", "new", "-m", "dependent work on top of conflict"],
        cwd=repo_dir,
        env=env,
    )
    with open(os.path.join(repo_dir, "monitor.cfg"), "w") as f:
        f.write("interval = 10s\n")
    res = run_cmd(["jj", "log", "-T", log_fmt], cwd=repo_dir, env=env)
    emit(res.stdout.strip())

    print("\n=== Step 7: Resolve Parent Conflict ===")
    run_cmd(["jj", "edit", "feature-b"], cwd=repo_dir, env=env)
    with open(os.path.join(repo_dir, "service.cfg"), "w") as f:
        f.write("port = 8080\nworkers = 24\ntimeout = 30\n")
    run_cmd(
        ["jj", "describe", "-m", "feature B: workers=24 (resolved)"],
        cwd=repo_dir,
        env=env,
    )
    res = run_cmd(["jj", "file", "list", "-T", tree_fmt], cwd=repo_dir, env=env)
    print("feature-b tree status:")
    emit(res.stdout.strip())

    print("\n=== Step 8: Auto-Rebased Child Commit Post-Resolution ===")
    res = run_cmd(["jj", "log", "-T", log_fmt], cwd=repo_dir, env=env)
    emit(res.stdout.strip())

    print("\n=== Step 9: Operation Log (Append-Only Transaction History) ===")
    op_fmt = 'description.first_line() ++ "\n"'
    res = run_cmd(
        ["jj", "op", "log", "-n", "6", "-T", op_fmt],
        cwd=repo_dir,
        env=env,
    )
    emit(res.stdout.strip())

    print("\n=== Step 10: Undo Resolution via Operation Log ===")
    # Undoing describe commit and edit commit returns feature-b to conflict state
    run_cmd(["jj", "undo"], cwd=repo_dir, env=env)
    run_cmd(["jj", "undo"], cwd=repo_dir, env=env)
    res = run_cmd(
        ["jj", "file", "list", "-r", "feature-b", "-T", tree_fmt],
        cwd=repo_dir,
        env=env,
    )
    print("feature-b restored to conflicted tree status:")
    emit(res.stdout.strip())


if __name__ == "__main__":
    main()
