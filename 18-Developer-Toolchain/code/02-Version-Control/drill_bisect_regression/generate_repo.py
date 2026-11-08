#!/usr/bin/env python3
"""Generate a deterministic 500-commit repository with a non-linear DAG."""

import os
import subprocess
import sys


def add_commit(stream, ref, mark, msg, from_mark=None, merge_mark=None, files=None):
    msg_b = msg.encode()
    stream.extend(f"commit {ref}\nmark :{mark}\ncommitter Git Drill Committer <drill@example.com> {1700000000 + mark*60} +0000\n".encode())
    stream.extend(f"data {len(msg_b)}\n".encode() + msg_b)
    if from_mark:
        stream.extend(f"from :{from_mark}\n".encode())
    if merge_mark:
        stream.extend(f"merge :{merge_mark}\n".encode())
    if files:
        for path, content in files.items():
            content_b = content.encode() if isinstance(content, str) else content
            stream.extend(f"M 644 inline {path}\n".encode())
            stream.extend(f"data {len(content_b)}\n".encode() + content_b)


def generate_repo(target_dir: str):
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Git Drill Committer"
    env["GIT_AUTHOR_EMAIL"] = "drill@example.com"
    env["GIT_COMMITTER_NAME"] = "Git Drill Committer"
    env["GIT_COMMITTER_EMAIL"] = "drill@example.com"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["LC_ALL"] = "C"

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target_dir, env=env, check=True)

    stream = bytearray()

    code_good = b"""#include <stdio.h>
#include <stdint.h>

// Baseline flat contiguous array lookup: 4200 ns (SLA <= 10000 ns)
uint64_t benchmark_latency(void) {
    return 4200;
}

int main(void) {
    uint64_t lat = benchmark_latency();
    printf("LATENCY_NS: %llu\\n", (unsigned long long)lat);
    return 0;
}
"""

    code_bad = b"""#include <stdio.h>
#include <stdint.h>

// Regressed dynamic linked-list traversal: 38500 ns (SLA violation)
uint64_t benchmark_latency(void) {
    return 38500;
}

int main(void) {
    uint64_t lat = benchmark_latency();
    printf("LATENCY_NS: %llu\\n", (unsigned long long)lat);
    return 0;
}
"""

    code_broken = b"""#include <stdio.h>
#include <stdint.h>

// UNBUILDABLE: syntax error on WIP branch
int main(void) {
    COMPILATION_ERROR_SYNTAX_TOKEN_UNTERMINATED;
    return 0;
}
"""

    makefile = b"""CC ?= cc
CFLAGS ?= -O3

all: order_matcher

order_matcher: order_matcher.c
\t$(CC) $(CFLAGS) -o order_matcher order_matcher.c

clean:
\trm -f order_matcher bench.out
"""

    # Commit 1: v1.0.0 baseline release
    add_commit(stream, "refs/heads/main", 1, "v1.0.0 baseline release\n", files={"order_matcher.c": code_good, "Makefile": makefile})
    stream.extend(b"reset refs/tags/v1.0.0\nfrom :1\n")

    mark = 2
    current_trunk = 1
    m12_step5 = None
    m13_step5 = None

    for b in range(1, 26):
        b_ref = f"refs/heads/feature_{b:02d}"
        b_parent = current_trunk

        if b == 12:
            # Build feature_12 up to step 5
            for c in range(1, 6):
                add_commit(stream, b_ref, mark, f"feat(12): worker commit {c}\n", from_mark=b_parent, files={"notes.txt": f"// f12 step {c}\n"})
                b_parent = mark
                mark += 1
            m12_step5 = b_parent

            # Build feature_13 up to step 5
            f13_parent = current_trunk
            for c in range(1, 6):
                add_commit(stream, "refs/heads/feature_13", mark, f"feat(13): worker commit {c}\n", from_mark=f13_parent, files={"notes.txt": f"// f13 step {c}\n"})
                f13_parent = mark
                mark += 1
            m13_step5 = f13_parent

            # Criss-cross merges:
            # feature_12 merges feature_13 step 5
            add_commit(stream, b_ref, mark, "feat(12): cross-merge feature_13 step 5\n", from_mark=b_parent, merge_mark=m13_step5)
            b_parent = mark
            mark += 1

            # feature_13 merges feature_12 step 5
            add_commit(stream, "refs/heads/feature_13", mark, "feat(13): cross-merge feature_12 step 5\n", from_mark=f13_parent, merge_mark=m12_step5)
            f13_parent = mark
            mark += 1

            # Finish remaining commits on feature_12 (steps 7 to 19)
            for c in range(7, 20):
                add_commit(stream, b_ref, mark, f"feat(12): worker commit {c}\n", from_mark=b_parent, files={"notes.txt": f"// f12 step {c}\n"})
                b_parent = mark
                mark += 1

            # Merge feature_12 into main
            merge_msg = "merge feature_12 into main\n"
            add_commit(stream, "refs/heads/main", mark, merge_msg, from_mark=current_trunk, merge_mark=b_parent, files={"order_matcher.c": code_good})
            current_trunk = mark
            mark += 1
            continue

        if b == 13:
            # Finish remaining commits on feature_13 (steps 7 to 19)
            for c in range(7, 20):
                add_commit(stream, b_ref, mark, f"feat(13): worker commit {c}\n", from_mark=f13_parent, files={"notes.txt": f"// f13 step {c}\n"})
                f13_parent = mark
                mark += 1

            # Merge feature_13 into main
            merge_msg = "merge feature_13 into main\n"
            add_commit(stream, "refs/heads/main", mark, merge_msg, from_mark=current_trunk, merge_mark=f13_parent, files={"order_matcher.c": code_good})
            current_trunk = mark
            mark += 1
            continue

        # Standard branch processing for all other branches
        for c in range(1, 20):
            is_regression = (b == 17 and c == 10)
            is_broken = (b == 17 and c == 8) or (b == 5 and c == 7)
            is_fix = (b == 17 and c == 9) or (b == 5 and c == 8)

            if is_regression:
                msg = "feat(routing): switch order lookup to dynamic linked list\n"
                files = {"order_matcher.c": code_bad}
            elif is_broken:
                msg = f"wip({b:02d}): incomplete refactoring commit {c}\n"
                files = {"order_matcher.c": code_broken}
            elif is_fix:
                msg = f"fix({b:02d}): fix build error from commit {c-1}\n"
                files = {"order_matcher.c": code_good}
            elif b > 17 or (b == 17 and c > 10):
                msg = f"feat({b:02d}): worker commit {c}\n"
                files = {"order_matcher.c": code_bad}
            else:
                msg = f"feat({b:02d}): worker commit {c}\n"
                files = {"notes.txt": f"// branch {b} step {c}\n"}

            add_commit(stream, b_ref, mark, msg, from_mark=b_parent, files=files)
            b_parent = mark
            mark += 1

        # Merge branch into trunk
        merge_msg = f"merge feature_{b:02d} into main\n"
        trunk_files = {"order_matcher.c": code_bad} if b >= 17 else {"order_matcher.c": code_good}
        add_commit(stream, "refs/heads/main", mark, merge_msg, from_mark=current_trunk, merge_mark=b_parent, files=trunk_files)
        current_trunk = mark
        mark += 1

    subprocess.run(["git", "fast-import", "--quiet"], input=stream, cwd=target_dir, env=env, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=target_dir, env=env, check=True)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    generate_repo(target)
