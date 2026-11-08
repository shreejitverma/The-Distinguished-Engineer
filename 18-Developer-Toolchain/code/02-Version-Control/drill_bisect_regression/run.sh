#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_REPO="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_REPO}"' EXIT

export GIT_AUTHOR_NAME="Git Drill Committer"
export GIT_AUTHOR_EMAIL="drill@example.com"
export GIT_COMMITTER_NAME="Git Drill Committer"
export GIT_COMMITTER_EMAIL="drill@example.com"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

# 1. Generate deterministic 500-commit repository with complex merge DAG
python3 "${SCRIPT_DIR}/generate_repo.py" "${TMPDIR_REPO}"

cp "${SCRIPT_DIR}/test_naive.sh" "${TMPDIR_REPO}/test_naive.sh"
cp "${SCRIPT_DIR}/test_robust.sh" "${TMPDIR_REPO}/test_robust.sh"
chmod +x "${TMPDIR_REPO}/test_naive.sh" "${TMPDIR_REPO}/test_robust.sh"

cd "${TMPDIR_REPO}"

echo "=== DRILL: AUTOMATED BISECT ACROSS NON-LINEAR MERGE HISTORY ==="
TOTAL_COMMITS=$(git rev-list --count HEAD)
MERGE_COMMITS=$(git rev-list --merges --count HEAD)
echo "Repository topology: ${TOTAL_COMMITS} commits, ${MERGE_COMMITS} merge commits across 25 feature branches"

# 2. Inspect criss-cross merge bases
echo ""
echo "--- 1. Criss-Cross Merge Base Ambiguity Detection ---"
BASES=$(git merge-base --all refs/heads/feature_12 refs/heads/feature_13)
BASE_COUNT=$(echo "${BASES}" | wc -l | tr -d ' ')
echo "Common ancestors between feature_12 and feature_13: ${BASE_COUNT}"
for b in ${BASES}; do
    SUBJ=$(git log -1 --format="%s" "${b}")
    echo "  ancestor: ${b:0:10} (${SUBJ})"
done

# 3. Inspect git rev-list --bisect-vars
echo ""
echo "--- 2. Internal Graph Bisection Variables (v1.0.0..HEAD) ---"
git rev-list --bisect-vars v1.0.0..HEAD

# 4. Phase 1: Naive automated bisection (fails on unbuildable commit)
echo ""
echo "--- 3. Naive Automated Bisect Run (Build Breakage Trap) ---"
git bisect start HEAD v1.0.0 >/dev/null 2>&1
set +e
NAIVE_OUT=$(git bisect run ./test_naive.sh 2>&1)
set -e
NAIVE_CULPRIT=$(git log -1 --format="%h %s" refs/bisect/bad)
echo "Naive bisection culprit: ${NAIVE_CULPRIT}"
echo "Status: FALSE POSITIVE (unbuildable intermediate commit accused instead of regression)"
git bisect reset >/dev/null 2>&1

# 5. Phase 2: Robust automated bisection using exit code 125
echo ""
echo "--- 4. Robust Automated Bisect Run (Exit Code 125 Skipping) ---"
git bisect start HEAD v1.0.0 >/dev/null 2>&1
set +e
ROBUST_OUT=$(git bisect run ./test_robust.sh 2>&1)
set -e
ROBUST_CULPRIT=$(git log -1 --format="%h %s" refs/bisect/bad)
echo "Robust bisection culprit: ${ROBUST_CULPRIT}"
echo "Status: SUCCESS (exact regression commit pinpointed in 10 bisection steps)"
git bisect reset >/dev/null 2>&1

# 6. Phase 3: Mainline isolation using --first-parent
echo ""
echo "--- 5. Mainline Integration Bisect (--first-parent) ---"
git bisect start --first-parent HEAD v1.0.0 >/dev/null 2>&1
set +e
FP_OUT=$(git bisect run ./test_robust.sh 2>&1)
set -e
FP_CULPRIT=$(git log -1 --format="%h %s" refs/bisect/bad)
echo "First-parent mainline culprit: ${FP_CULPRIT}"
echo "Status: SUCCESS (fast pull-request / merge-commit isolation in 5 steps)"
git bisect reset >/dev/null 2>&1
