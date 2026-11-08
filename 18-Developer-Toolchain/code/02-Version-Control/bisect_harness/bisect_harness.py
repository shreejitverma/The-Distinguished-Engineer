#!/usr/bin/env python3
"""Git Archaeology Verification Lab: Log, Blame, and Automated Bisect.

This harness constructs a deterministic Git repository to verify:
1. Automated non-linear DAG bisection using git bisect run with exit codes (0, 1, 125).
2. Skip handling when encountering untestable revisions.
3. Pickaxe search mechanics: -S (occurrence counting) versus -G (diff hunk regex).
4. Attribution tracking with blame ignore-revs and cross-file copy detection (-C -C).
5. Patch series equivalence with git range-diff (Jonker-Volgenant LSAP algorithm).
6. Non-destructive metadata attachment with git notes (refs/notes/commits).
7. Single-pass directory tree archaeology with the experimental git last-modified command.
"""

import os
import re
import subprocess
import sys
import tempfile

MIN_GIT = (2, 52)  # git last-modified and range-diff --max-memory arrive in 2.52


def check_git_version() -> None:
    out = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, text=True, check=True).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    if m is None or (int(m.group(1)), int(m.group(2))) < MIN_GIT:
        sys.exit(f"this lab needs git >= {MIN_GIT[0]}.{MIN_GIT[1]}, found: {out.strip()}")


def main() -> None:
    check_git_version()
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "Toolchain Author"
        env["GIT_AUTHOR_EMAIL"] = "author@example.com"
        env["GIT_COMMITTER_NAME"] = "Toolchain Committer"
        env["GIT_COMMITTER_EMAIL"] = "committer@example.com"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        env["LC_ALL"] = "C"

        def run_cmd(*args: str, check: bool = True) -> str:
            res = subprocess.run(
                args,
                cwd=tmpdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if res.returncode != 0 and check:
                raise RuntimeError(
                    f"Command failed ({res.returncode}): {' '.join(args)}\n{res.stdout}"
                )
            return res.stdout

        def set_commit_time(time_str: str) -> None:
            iso = f"2026-10-01T{time_str}Z"
            env["GIT_AUTHOR_DATE"] = iso
            env["GIT_COMMITTER_DATE"] = iso

        # Initialize repository
        run_cmd("git", "init", "-b", "main", "-q")

        # C0: Root baseline commit
        set_commit_time("10:00:00")
        with open(os.path.join(tmpdir, "calc.py"), "w", encoding="ascii") as f:
            f.write(
                "# Calculator engine module\n"
                "FACTOR_SCALE = 10\n"
                "\n"
                "def compute_metrics(base, multiplier, offset):\n"
                "    val = (base * multiplier) + offset\n"
                "    if val > 1000:\n"
                "        return val // 2\n"
                "    return val * 2\n"
                "\n"
                "def compute(x):\n"
                "    return x * FACTOR_SCALE\n"
            )
        with open(os.path.join(tmpdir, "util.py"), "w", encoding="ascii") as f:
            f.write(
                "# Utility module\n"
                "def helper():\n"
                "    return 42\n"
            )
        run_cmd("git", "add", ".")
        run_cmd("git", "commit", "-m", "c0: initial codebase", "-q")
        c0 = run_cmd("git", "rev-parse", "HEAD").strip()

        # C1: Feature branch commit
        run_cmd("git", "checkout", "-b", "feat", "-q")
        set_commit_time("10:05:00")
        with open(os.path.join(tmpdir, "extra.py"), "w", encoding="ascii") as f:
            f.write(
                "# Extra module\n"
                "def extra_fn():\n"
                "    return 100\n"
            )
        run_cmd("git", "add", "extra.py")
        run_cmd("git", "commit", "-m", "c1: add extra module", "-q")

        # C2: Mainline progress
        run_cmd("git", "checkout", "main", "-q")
        set_commit_time("10:10:00")
        with open(os.path.join(tmpdir, "util.py"), "a", encoding="ascii") as f:
            f.write(
                "def logger(msg):\n"
                "    pass\n"
            )
        run_cmd("git", "commit", "-am", "c2: add logger", "-q")

        # C3: Merge feature branch into mainline
        set_commit_time("10:15:00")
        run_cmd("git", "merge", "feat", "--no-ff", "-m", "c3: merge feat branch", "-q")

        # C4: Clean refactor
        set_commit_time("10:20:00")
        with open(os.path.join(tmpdir, "calc.py"), "w", encoding="ascii") as f:
            f.write(
                "# Calculator engine module\n"
                "FACTOR_SCALE = 10\n"
                "\n"
                "def compute_metrics(base, multiplier, offset):\n"
                "    val = (base * multiplier) + offset\n"
                "    if val > 1000:\n"
                "        return val // 2\n"
                "    return val * 2\n"
                "\n"
                "def validate(x):\n"
                "    return x > 0\n"
                "\n"
                "def compute(x):\n"
                "    return x * FACTOR_SCALE\n"
            )
        run_cmd("git", "commit", "-am", "c4: add input validation", "-q")

        # C5: Culprit commit (silent regression introduced in compute)
        set_commit_time("10:25:00")
        with open(os.path.join(tmpdir, "calc.py"), "w", encoding="ascii") as f:
            f.write(
                "# Calculator engine module\n"
                "FACTOR_SCALE = 10\n"
                "\n"
                "def compute_metrics(base, multiplier, offset):\n"
                "    val = (base * multiplier) + offset\n"
                "    if val > 1000:\n"
                "        return val // 2\n"
                "    return val * 2\n"
                "\n"
                "def validate(x):\n"
                "    return x > 0\n"
                "\n"
                "def compute(x):\n"
                "    # Regression: accidental subtraction\n"
                "    return x - FACTOR_SCALE\n"
            )
        run_cmd("git", "commit", "-am", "c5: optimize calculation path", "-q")
        c5_culprit = run_cmd("git", "rev-parse", "HEAD").strip()

        # C6: Cosmetic update (modifies FACTOR_SCALE line without changing count)
        set_commit_time("10:30:00")
        with open(os.path.join(tmpdir, "calc.py"), "w", encoding="ascii") as f:
            f.write(
                "# Calculator engine module\n"
                "FACTOR_SCALE = 10  # constant scaling factor\n"
                "\n"
                "def compute_metrics(base, multiplier, offset):\n"
                "    val = (base * multiplier) + offset\n"
                "    if val > 1000:\n"
                "        return val // 2\n"
                "    return val * 2\n"
                "\n"
                "def validate(x):\n"
                "    return x > 0\n"
                "\n"
                "def compute(x):\n"
                "    # Regression: accidental subtraction\n"
                "    return x - FACTOR_SCALE\n"
            )
        run_cmd("git", "commit", "-am", "c6: annotate factor scale", "-q")

        # C7: Automated bot formatting commit (reformats compute_metrics body)
        env["GIT_AUTHOR_NAME"] = "Bot Formatter"
        env["GIT_AUTHOR_EMAIL"] = "bot@example.com"
        env["GIT_COMMITTER_NAME"] = "Bot Formatter"
        env["GIT_COMMITTER_EMAIL"] = "bot@example.com"
        set_commit_time("10:35:00")
        with open(os.path.join(tmpdir, "calc.py"), "w", encoding="ascii") as f:
            f.write(
                "# Calculator engine module\n"
                "FACTOR_SCALE = 10  # constant scaling factor\n"
                "\n"
                "def compute_metrics(base, multiplier, offset):\n"
                "    val = (base * multiplier) + offset\n"
                "    if val > 1000: return val // 2\n"
                "    return val * 2\n"
                "\n"
                "def validate(x):\n"
                "    return x > 0\n"
                "\n"
                "def compute(x):\n"
                "    # Regression: accidental subtraction\n"
                "    return x - FACTOR_SCALE\n"
            )
        run_cmd("git", "commit", "-am", "c7: automated formatting of one-line branches", "-q")
        c7_bot = run_cmd("git", "rev-parse", "HEAD").strip()

        # C8: HEAD documentation commit
        env["GIT_AUTHOR_NAME"] = "Toolchain Author"
        env["GIT_AUTHOR_EMAIL"] = "author@example.com"
        env["GIT_COMMITTER_NAME"] = "Toolchain Committer"
        env["GIT_COMMITTER_EMAIL"] = "committer@example.com"
        set_commit_time("10:40:00")
        with open(os.path.join(tmpdir, "README.md"), "w", encoding="ascii") as f:
            f.write("# Project Archaeology\n")
        run_cmd("git", "add", "README.md")
        run_cmd("git", "commit", "-m", "c8: add project readme", "-q")
        c8_head = run_cmd("git", "rev-parse", "HEAD").strip()

        # Branch feat-v2 for range-diff comparison
        run_cmd("git", "checkout", "-b", "feat-v2", "feat", "-q")
        set_commit_time("10:45:00")
        with open(os.path.join(tmpdir, "extra.py"), "w", encoding="ascii") as f:
            f.write(
                "# Extra module\n"
                "def extra_fn():\n"
                "    # optimized implementation\n"
                "    return 100\n"
            )
        run_cmd(
            "git",
            "commit",
            "-a",
            "--amend",
            "-m",
            "c1-v2: add extra module with optimization",
            "-q",
        )
        run_cmd("git", "checkout", "main", "-q")

        # C9: Cross-file copy of compute_metrics into fast_calc.py
        set_commit_time("10:50:00")
        with open(os.path.join(tmpdir, "fast_calc.py"), "w", encoding="ascii") as f:
            f.write(
                "# Fast calculator standalone module\n"
                "def compute_metrics(base, multiplier, offset):\n"
                "    val = (base * multiplier) + offset\n"
                "    if val > 1000:\n"
                "        return val // 2\n"
                "    return val * 2\n"
            )
        run_cmd("git", "add", "fast_calc.py")
        run_cmd("git", "commit", "-m", "c9: copy compute_metrics to fast_calc.py", "-q")

        # -------------------------------------------------------------
        # Part 1: Automated Non-Linear Bisect Run
        # -------------------------------------------------------------
        print("=== 1. Automated Bisect Run ===")
        test_sh = os.path.join(tmpdir, "test_harness.sh")
        with open(test_sh, "w", encoding="ascii") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                "python3 -m py_compile calc.py 2>/dev/null || exit 125\n"
                "python3 -c '\n"
                "import sys, calc\n"
                "val = calc.compute(5)\n"
                "if val != 50:\n"
                "    sys.exit(1)\n"
                "sys.exit(0)\n"
                "'\n"
            )
        os.chmod(test_sh, 0o755)

        run_cmd("git", "bisect", "start", c8_head, c0, "--")
        bisect_out = run_cmd("git", "bisect", "run", "./test_harness.sh")
        steps = [ln for ln in bisect_out.splitlines() if ln.startswith("Bisecting:")]
        culprit = run_cmd("git", "rev-parse", "refs/bisect/bad").strip()
        first_bad_summary = run_cmd("git", "log", "-1", "--format=%h %s", culprit).strip()
        print(f"Bisect steps: {len(steps)}")
        for ln in steps:
            print(f"  {ln}")
        print(f"First bad commit: {first_bad_summary}")
        print(f"Culprit matches c5: {culprit == c5_culprit}")
        run_cmd("git", "bisect", "reset")

        # -------------------------------------------------------------
        # Part 2: Bisect Skip with Exit Code 125
        # -------------------------------------------------------------
        print("\n=== 2. Bisect Skip Handling (Exit Code 125) ===")
        run_cmd("git", "checkout", "-b", "skip-demo", c0, "-q")
        set_commit_time("11:00:00")
        with open(os.path.join(tmpdir, "calc.py"), "w", encoding="ascii") as f:
            f.write("def compute(x)\n    return x * 10\n")  # Syntax error
        run_cmd("git", "commit", "-am", "s1: broken syntax commit", "-q")
        s1_broken = run_cmd("git", "rev-parse", "HEAD").strip()

        set_commit_time("11:05:00")
        with open(os.path.join(tmpdir, "calc.py"), "w", encoding="ascii") as f:
            f.write("def compute(x):\n    return x - 10\n")  # Regression introduced
        run_cmd("git", "commit", "-am", "s2: regression commit", "-q")
        s2_bad = run_cmd("git", "rev-parse", "HEAD").strip()

        run_cmd("git", "bisect", "start", s2_bad, c0, "--")
        skip_out = run_cmd("git", "bisect", "run", "./test_harness.sh", check=False)
        print("Bisect skip status: bounded uncertainty set detected")
        lines = skip_out.splitlines()
        candidates: list[str] = []
        for i, line in enumerate(lines):
            if "The first 'bad' commit could be any of:" in line or "We cannot bisect more!" in line:
                print(f"  {line.strip()}")
            if "The first 'bad' commit could be any of:" in line:
                # the candidate hashes follow, one per line, until the blank line
                for cand in lines[i + 1 :]:
                    if not re.fullmatch(r"[0-9a-f]{40}", cand.strip()):
                        break
                    candidates.append(cand.strip())
        subjects = sorted(run_cmd("git", "log", "-1", "--format=%s", c).strip() for c in candidates)
        print(f"Candidate set size: {len(candidates)}")
        for subj in subjects:
            print(f"  candidate: {subj}")
        print(f"Candidates equal {{s1, s2}}: {set(candidates) == {s1_broken, s2_bad}}")
        run_cmd("git", "bisect", "reset")
        run_cmd("git", "checkout", "main", "-q")

        # -------------------------------------------------------------
        # Part 3: Pickaxe Search (-S vs -G)
        # -------------------------------------------------------------
        print("\n=== 3. Pickaxe Search (-S vs -G) ===")
        s_out = run_cmd("git", "log", "-S", "FACTOR_SCALE = 10", "--format=%h %s", c8_head)
        print("Pickaxe -S (occurrence count changes):")
        for line in s_out.strip().splitlines():
            print(f"  {line}")

        g_out = run_cmd("git", "log", "-G", "FACTOR_SCALE = 10", "--format=%h %s", c8_head)
        print("Pickaxe -G (diff hunk regex matches):")
        for line in g_out.strip().splitlines():
            print(f"  {line}")

        # -------------------------------------------------------------
        # Part 4: Blame Ignore-Revs and Cross-File Copy Detection
        # -------------------------------------------------------------
        print("\n=== 4. Blame with Ignore-Revs and Copy Detection ===")
        blame_normal = run_cmd("git", "blame", "--show-email", "-L", "4,7", "calc.py")
        print("Blame normal (calc.py L4-7, line 6 attributed to bot):")
        for line in blame_normal.strip().splitlines():
            print(f"  {line}")

        blame_ignore = run_cmd(
            "git",
            "-c",
            "blame.markIgnoredLines=true",
            "blame",
            "--show-email",
            "--ignore-rev",
            c7_bot,
            "-L",
            "4,7",
            "calc.py",
        )
        print("Blame with --ignore-rev (calc.py L4-7, line 6 restored with marker):")
        for line in blame_ignore.strip().splitlines():
            print(f"  {line}")

        blame_single = run_cmd("git", "blame", "-f", "-C", "-L", "2,6", "fast_calc.py")
        print("Blame with a single -C (fast_calc.py L2-6, copy from calc.py is missed):")
        for line in blame_single.strip().splitlines():
            print(f"  {line}")

        blame_copy = run_cmd("git", "blame", "-f", "-C", "-C", "-L", "2,6", "fast_calc.py")
        print("Blame with -C -C cross-file copy detection (fast_calc.py L2-6):")
        for line in blame_copy.strip().splitlines():
            print(f"  {line}")

        # -------------------------------------------------------------
        # Part 5: Range-Diff (Patch Series Evolution)
        # -------------------------------------------------------------
        print("\n=== 5. Range-Diff (Patch Series Evolution) ===")
        rd_out = run_cmd("git", "range-diff", "--no-color", "main~8..feat", "main~8..feat-v2")
        print("Range-diff output:")
        for line in rd_out.strip().splitlines():
            print(f"  {line}")

        # -------------------------------------------------------------
        # Part 6: Non-Destructive Git Notes
        # -------------------------------------------------------------
        print("\n=== 6. Non-Destructive Git Notes ===")
        tip_before = run_cmd("git", "rev-parse", "HEAD").strip()
        commit_before = run_cmd("git", "cat-file", "commit", culprit)
        run_cmd(
            "git",
            "notes",
            "add",
            "-m",
            "Audit: verified regression via automated bisect harness",
            culprit,
        )
        note_log = run_cmd("git", "log", "-1", "--notes", culprit)
        print("Commit with attached note:")
        for line in note_log.strip().splitlines():
            print(f"  {line}")
        commit_after = run_cmd("git", "cat-file", "commit", culprit)
        plain_log = run_cmd("git", "log", "-1", "--no-notes", culprit)
        notes_ref_list = run_cmd("git", "notes", "list").split()
        print(f"Commit object bytes unchanged: {commit_before == commit_after}")
        print(f"Branch tip unchanged: {run_cmd('git', 'rev-parse', 'HEAD').strip() == tip_before}")
        print(f"Plain log without notes shows no note: {'Notes:' not in plain_log}")
        print(f"Notes ref holds one note for the culprit: {len(notes_ref_list) == 2 and notes_ref_list[1] == culprit}")

        # -------------------------------------------------------------
        # Part 7: Tree Archaeology with git last-modified
        # -------------------------------------------------------------
        print("\n=== 7. Tree Archaeology with git last-modified ===")
        lm_out = run_cmd("git", "last-modified")
        print("Last-modified directory listing:")
        for line in sorted(lm_out.strip().splitlines(), key=lambda l: l.split("\t")[1]):
            oid, path = line.split("\t")
            short_oid = run_cmd("git", "rev-parse", "--short", oid).strip()
            print(f"  {short_oid} {path}")


if __name__ == "__main__":
    main()
