#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_LAB="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_LAB}"' EXIT

export GIT_AUTHOR_NAME="Toolchain Author"
export GIT_AUTHOR_EMAIL="author@example.com"
export GIT_COMMITTER_NAME="Toolchain Committer"
export GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_AUTHOR_DATE="2026-11-01T12:00:00Z"
export GIT_COMMITTER_DATE="2026-11-01T12:00:00Z"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

# git merge-tree --write-tree needs 2.38 and merge-file --zdiff3 needs 2.35; fail loudly rather than print a different lab.
GIT_VER="$(git version | awk '{print $3}')"
if [ "$(printf '%s\n2.38.0\n' "${GIT_VER}" | sort -V | head -n1)" != "2.38.0" ]; then
	echo "git >= 2.38 required, found ${GIT_VER}" >&2
	exit 1
fi

cd "${TMPDIR_LAB}"

echo "=== 1. Diff Algorithms: Myers vs Histogram (Frobnitz Test) ==="
cat > file1.c << "EOF"
#include <stdio.h>

// Frobs foo heartily.
int frobnitz(int foo)
{
	int i;
	for(i = 0; i < 10; i++)
	{
		printf("Your answer is: ");
		printf("%d\n", foo);
	}
}

int fact(int n)
{
	if(n > 1)
	{
		return fact(n-1) * n;
	}
	return 1;
}

int main(int argc, char **argv)
{
	frobnitz(fact(10));
}
EOF

cat > file2.c << "EOF"
#include <stdio.h>

int fib(int n)
{
	if(n > 2)
	{
		return fib(n-1) + fib(n-2);
	}
	return 1;
}

// Frobs foo heartily.
int frobnitz(int foo)
{
	int i;
	for(i = 0; i < 10; i++)
	{
		printf("%d\n", foo);
	}
}

int main(int argc, char **argv)
{
	frobnitz(fib(10));
}
EOF

echo "--- Git Myers Diff ---"
git diff --no-index --diff-algorithm=myers file1.c file2.c || true

echo "--- Git Histogram Diff ---"
git diff --no-index --diff-algorithm=histogram file1.c file2.c || true

echo "--- Git Diff Size per Algorithm (git diff --numstat: added deleted) ---"
for algo in myers patience histogram; do
	printf '%-9s ' "${algo}"
	git diff --no-index --numstat --diff-algorithm="${algo}" file1.c file2.c | awk '{print $1, $2}' || true
done

echo "--- Reference Myers & Anchor Analysis ---"
python3 "${SCRIPT_DIR}/merge_diff.py" diff file1.c file2.c

echo ""
echo "=== 2. Criss-Cross Merge & Multiple Merge Bases ==="
git init -b main -q
git config user.name "Toolchain Author"
git config user.email "author@example.com"
git config merge.conflictStyle zdiff3
git config rerere.enabled true

echo "line 1" > file1.txt
echo "line 2" > file2.txt
git add file1.txt file2.txt
git commit -m "commit A: root" -q
git tag commit-A

git checkout -b branch1 -q
echo "branch 1 edit" >> file1.txt
git commit -am "commit B: branch1 edit" -q
git tag commit-B

git checkout main -q
git checkout -b branch2 -q
echo "branch 2 edit" >> file2.txt
git commit -am "commit C: branch2 work" -q
git tag commit-C

# Branch 1 merges Branch 2 (commit D)
git checkout branch1 -q
export GIT_AUTHOR_DATE="2026-11-01T12:01:00Z"
export GIT_COMMITTER_DATE="2026-11-01T12:01:00Z"
git merge branch2 -m "commit D: merge branch2 into branch1" -q

# Branch 2 merges prior commit of Branch 1 (commit E)
git checkout branch2 -q
export GIT_AUTHOR_DATE="2026-11-01T12:02:00Z"
export GIT_COMMITTER_DATE="2026-11-01T12:02:00Z"
git merge commit-B -m "commit E: merge branch1 prior into branch2" -q

# Later work on each side, so the final merge must combine two new edits on top of the criss-cross
export GIT_AUTHOR_DATE="2026-11-01T12:03:00Z"
export GIT_COMMITTER_DATE="2026-11-01T12:03:00Z"
echo "branch 2 later" >> file2.txt
git commit -am "commit G: branch2 later edit" -q
git checkout branch1 -q
export GIT_AUTHOR_DATE="2026-11-01T12:04:00Z"
export GIT_COMMITTER_DATE="2026-11-01T12:04:00Z"
echo "branch 1 later" >> file1.txt
git commit -am "commit F: branch1 later edit" -q

echo "Merge bases between branch1 (F, child of D) and branch2 (G, child of E):"
git merge-base --all branch1 branch2

echo "--- Virtual Merge Base Made Explicit ---"
# ORT merges the merge bases B and C over their own merge base A; the result is the virtual ancestor.
VIRT_TREE=$(git merge-tree --write-tree --merge-base=commit-A commit-B commit-C)
echo "Virtual ancestor = merge(B, C) over A, tree ${VIRT_TREE}:"
for f in file1.txt file2.txt; do
	echo "  ${f}: $(git show "${VIRT_TREE}:${f}" | tr '\n' '|')"
done
echo "Commit B alone: file2.txt = $(git show commit-B:file2.txt | tr '\n' '|')"
echo "Commit C alone: file1.txt = $(git show commit-C:file1.txt | tr '\n' '|')"

echo ""
echo "=== 3. In-Core Worktree-Free Merge (git merge-tree) ==="
# Test clean worktree-free merge
TREE_CLEAN=$(git merge-tree --write-tree branch1 branch2)
echo "Clean merge-tree synthesized OID: ${TREE_CLEAN}"
for f in file1.txt file2.txt; do
	echo "  ${f}: $(git show "${TREE_CLEAN}:${f}" | tr '\n' '|')"
done

echo ""
echo "=== 4. Conflict Markers (zdiff3) and Binary Index Stage Extraction ==="
git checkout -b feat_conflict_a main -q
cat > service.conf << "EOF"
# Global Config
service_name = payment_gateway
port = 8080
max_workers = 16
timeout_sec = 30
EOF
git add service.conf
git commit -m "base service configuration" -q

git checkout -b feat_conflict_b -q
cat > service.conf << "EOF"
# Global Config
service_name = payment_gateway
port = 9090
max_workers = 16
timeout_sec = 30
EOF
git commit -am "update port to 9090" -q

git checkout feat_conflict_a -q
cat > service.conf << "EOF"
# Global Config
service_name = payment_gateway
port = 7070
max_workers = 16
timeout_sec = 30
EOF
git commit -am "update port to 7070" -q

echo "Triggering three-way merge conflict:"
git merge feat_conflict_b 2>&1 || true

echo "--- Conflicted File Markers (zdiff3) ---"
cat service.conf

echo "--- Binary Index Stage Extraction (.git/index) ---"
python3 "${SCRIPT_DIR}/merge_diff.py" index .git/index

echo "--- zdiff3 vs diff3: common lines at the edge of a conflict (git merge-file) ---"
printf 'start\nbase\nend\n' > zbase.txt
printf 'start\nmine\nshared\nend\n' > zours.txt
printf 'start\ntheirs\nshared\nend\n' > ztheirs.txt
for style in diff3 zdiff3; do
	echo "[${style}]"
	git merge-file -p "--${style}" -L ours -L base -L theirs zours.txt zbase.txt ztheirs.txt || true
done

echo "--- Resolving Conflict via git add ---"
cat > service.conf << "EOF"
# Global Config
service_name = payment_gateway
port = 8443
max_workers = 16
timeout_sec = 30
EOF
git add service.conf

echo "Binary Index Stage Inspection After Resolution:"
python3 "${SCRIPT_DIR}/merge_diff.py" index .git/index
git commit -m "resolved port conflict to 8443" -q

echo ""
echo "=== 5. Directory Rename Detection (merge.directoryRenames) ==="
mkdir -p src/legacy
echo "int util(void) { return 0; }" > src/legacy/util.c
echo "int worker(void) { return 1; }" > src/legacy/worker.c
git add src/legacy
git commit -m "setup legacy directory structure" -q

git checkout -b dir_rename_branch -q
git mv src/legacy src/core
git commit -m "rename src/legacy to src/core" -q

git checkout feat_conflict_a -q
git checkout -b dir_add_branch -q
echo "int telemetry(void) { return 42; }" > src/legacy/telemetry.c
git add src/legacy/telemetry.c
git commit -m "add telemetry.c in src/legacy" -q

for mode in false conflict; do
	echo "Merging with merge.directoryRenames=${mode} (then aborting):"
	git -c "merge.directoryRenames=${mode}" merge --no-commit --no-ff dir_rename_branch 2>&1 || true
	echo "Unmerged index entries (mode stage path):"
	git ls-files -u | awk '{print $1, $3, $4}'
	echo "Telemetry path in worktree:"
	ls src/*/telemetry.c
	git merge --abort
done

echo "Merging with merge.directoryRenames=true:"
git -c merge.directoryRenames=true merge dir_rename_branch -m "merge dir rename" -q 2>&1
ls -1 src/core

echo ""
echo "=== 6. Reuse Recorded Resolution (git rerere) ==="
# Trigger conflict on a fresh branch to demonstrate rerere replay
git checkout -b r_branch1 main -q
cat > rate_limit.conf << "EOF"
burst = 100
rate = 10
EOF
git add rate_limit.conf
git commit -m "base rate limit" -q

git checkout -b r_branch2 -q
cat > rate_limit.conf << "EOF"
burst = 200
rate = 10
EOF
git commit -am "burst 200" -q

git checkout r_branch1 -q
cat > rate_limit.conf << "EOF"
burst = 300
rate = 10
EOF
git commit -am "burst 300" -q

echo "Triggering merge to create initial rerere record:"
git merge r_branch2 2>&1 || true

echo "Resolving conflict and recording postimage:"
cat > rate_limit.conf << "EOF"
burst = 500
rate = 10
EOF
git rerere 2>&1
git add rate_limit.conf
git commit -m "resolved burst to 500" -q

echo "--- rr-cache Verification ---"
python3 "${SCRIPT_DIR}/merge_diff.py" rr-cache .git

echo "--- Resetting and Re-merging to Test Rerere Replay ---"
git reset --hard HEAD~1 -q
git merge r_branch2 2>&1 || true
echo "File content auto-resolved by rerere:"
cat rate_limit.conf
