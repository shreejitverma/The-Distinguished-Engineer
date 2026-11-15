#!/usr/bin/env bash
# Environment requirements: none beyond bash, git, python3, and a C compiler (${CC:-cc}); no network, container, or PMU.
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

# 1. Generate deterministic 501-commit repository with complex merge DAG
python3 "${SCRIPT_DIR}/generate_repo.py" "${TMPDIR_REPO}"

cp "${SCRIPT_DIR}/test_naive.sh" "${TMPDIR_REPO}/test_naive.sh"
cp "${SCRIPT_DIR}/test_robust.sh" "${TMPDIR_REPO}/test_robust.sh"
chmod +x "${TMPDIR_REPO}/test_naive.sh" "${TMPDIR_REPO}/test_robust.sh"

cd "${TMPDIR_REPO}"

echo "=== DRILL: AUTOMATED BISECT ACROSS NON-LINEAR MERGE HISTORY ==="
TOTAL_COMMITS=$(git rev-list --count HEAD)
MERGE_COMMITS=$(git rev-list --merges --count HEAD)
FEATURE_BRANCHES=$(git for-each-ref --format='%(refname)' 'refs/heads/feature_*' | wc -l | tr -d ' ')
echo "Repository topology: ${TOTAL_COMMITS} commits, ${MERGE_COMMITS} merge commits across ${FEATURE_BRANCHES} feature branches"

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

# Ground truth, computed from the generated history rather than assumed:
# the one commit that carries the latency regression, and the trunk merge that brought it in.
TRUTH=$(git log --format=%h --grep='^feat(routing): switch order lookup' main)
TRUTH_MERGE=""
for m in $(git rev-list --first-parent --reverse main); do
    if git merge-base --is-ancestor "${TRUTH}" "${m}"; then
        TRUTH_MERGE=$(git rev-parse --short "${m}")
        break
    fi
done

# Summarize the bisect session recorded in `git bisect log`.
# Every tested revision is one good, bad, or skip line; `start` with both endpoints records none.
bisect_tested() {
    local log good bad skip
    log=$(git bisect log)
    good=$(printf '%s\n' "${log}" | grep -c '^git bisect good ' || true)
    bad=$(printf '%s\n' "${log}" | grep -c '^git bisect bad ' || true)
    skip=$(printf '%s\n' "${log}" | grep -c '^git bisect skip ' || true)
    echo "$((good + bad + skip)) revisions tested: ${good} good, ${bad} bad, ${skip} skip"
}

# A culprit is judged by what it does, not by a fixed label: does its order_matcher.c compile?
builds() {
    git show "$1:order_matcher.c" | ${CC:-cc} -fsyntax-only -x c - >/dev/null 2>&1
}

# 4. Phase 1: Naive automated bisection (fails on unbuildable commit)
echo ""
echo "--- 3. Naive Automated Bisect Run (Build Breakage Trap) ---"
git bisect start HEAD v1.0.0 >/dev/null 2>&1
set +e
NAIVE_OUT=$(git bisect run ./test_naive.sh 2>&1)
set -e
NAIVE_BAD=$(git rev-parse --short refs/bisect/bad)
NAIVE_CULPRIT=$(git log -1 --format="%h %s" refs/bisect/bad)
NAIVE_TESTED=$(bisect_tested)
git bisect reset >/dev/null 2>&1
echo "Naive bisection culprit: ${NAIVE_CULPRIT}"
echo "Naive session: ${NAIVE_TESTED}"
if [ "${NAIVE_BAD}" = "${TRUTH}" ]; then
    echo "Verdict: CORRECT (culprit is the regression commit)"
elif ! builds "${NAIVE_BAD}"; then
    echo "Verdict: FALSE POSITIVE (culprit does not compile, so it cannot have introduced a latency regression)"
else
    echo "Verdict: WRONG COMMIT (culprit compiles but is not the regression commit)"
fi

# 5. Phase 2: Robust automated bisection using exit code 125
echo ""
echo "--- 4. Robust Automated Bisect Run (Exit Code 125 Skipping) ---"
git bisect start HEAD v1.0.0 >/dev/null 2>&1
set +e
ROBUST_OUT=$(git bisect run ./test_robust.sh 2>&1)
set -e
ROBUST_BAD=$(git rev-parse --short refs/bisect/bad)
ROBUST_CULPRIT=$(git log -1 --format="%h %s" refs/bisect/bad)
ROBUST_TESTED=$(bisect_tested)
git bisect reset >/dev/null 2>&1
echo "Robust bisection culprit: ${ROBUST_CULPRIT}"
echo "Robust session: ${ROBUST_TESTED}"
if [ "${ROBUST_BAD}" = "${TRUTH}" ]; then
    echo "Verdict: CORRECT (culprit is the regression commit)"
else
    echo "Verdict: WRONG COMMIT (expected ${TRUTH})"
fi

# 6. Phase 3: Mainline isolation using --first-parent
echo ""
echo "--- 5. Mainline Integration Bisect (--first-parent) ---"
git bisect start --first-parent HEAD v1.0.0 >/dev/null 2>&1
set +e
FP_OUT=$(git bisect run ./test_robust.sh 2>&1)
set -e
FP_BAD=$(git rev-parse --short refs/bisect/bad)
FP_CULPRIT=$(git log -1 --format="%h %s" refs/bisect/bad)
FP_TESTED=$(bisect_tested)
git bisect reset >/dev/null 2>&1
echo "First-parent mainline culprit: ${FP_CULPRIT}"
echo "First-parent session: ${FP_TESTED}"
if [ "${FP_BAD}" = "${TRUTH_MERGE}" ]; then
    echo "Verdict: CORRECT (culprit is the trunk merge that brought in the regression)"
else
    echo "Verdict: WRONG COMMIT (expected ${TRUTH_MERGE})"
fi

# 7. Phase 4: merge-base check. A good commit that is not an ancestor of bad makes Git test the merge bases first.
echo ""
echo "--- 6. Merge-Base Check (good is not an ancestor of bad) ---"
START_OUT=$(git bisect start refs/heads/feature_13 refs/heads/feature_12 2>&1)
if printf '%s\n' "${START_OUT}" | grep -q 'Bisecting: a merge base must be tested'; then
    echo "Prompt 'a merge base must be tested': yes"
else
    echo "Prompt 'a merge base must be tested': no"
fi
FIRST_BASE=$(git rev-parse HEAD)
if printf '%s\n' "${BASES}" | grep -qx "${FIRST_BASE}"; then
    echo "First revision checked out is a merge base: yes (${FIRST_BASE:0:10})"
else
    echo "First revision checked out is a merge base: no (${FIRST_BASE:0:10})"
fi
set +e
BAD_OUT=$(git bisect bad 2>&1)
BAD_RC=$?
set -e
echo "Marking that merge base bad: exit status ${BAD_RC}"
printf '%s\n' "${BAD_OUT}" | sed -E 's/[0-9a-f]{40}/<hash>/g'
git bisect reset >/dev/null 2>&1
