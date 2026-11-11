#!/usr/bin/env bash
set -euo pipefail

# Deterministic environment for reproducible Git SHA-1 hashes
export GIT_AUTHOR_NAME="Toolchain Recovery"
export GIT_AUTHOR_EMAIL="recovery@example.com"
export GIT_COMMITTER_NAME="Toolchain Recovery"
export GIT_COMMITTER_EMAIL="recovery@example.com"
export GIT_AUTHOR_DATE="2026-10-25T12:00:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:00:00Z"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export LC_ALL=C
export LANG=C

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# Every printed claim is read back from Git and asserted; a failed assertion
# aborts the lab with a nonzero exit, so no line is ever an unconditional echo.
die() {
    echo "LAB FAILURE: $*" >&2
    exit 1
}

# expect_eq <label> <actual> <expected>
expect_eq() {
    if [ "$2" != "$3" ]; then
        die "$1: got '$2', expected '$3'"
    fi
}

cd "${TMP_DIR}"
git init --object-format=sha1 -b main -q

# ==============================================================================
# Scenario 1: Recover Commit Lost via Hard Reset (Reflog Walk)
# ==============================================================================
echo "=== Step 1: Recover Commit Lost via Hard Reset (Reflog Walk) ==="

echo "Base system initialization" > base.txt
git add base.txt
git commit -m "Initial commit" -q
C1_HASH="$(git rev-parse --short HEAD)"
echo "Initial commit created: ${C1_HASH} Initial commit"

export GIT_AUTHOR_DATE="2026-10-25T12:01:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:01:00Z"
echo "Critical kernel patch" > feature.txt
git add feature.txt
git commit -m "Critical feature patch" -q
C2_FULL="$(git rev-parse HEAD)"
C2_SHORT="$(git rev-parse --short HEAD)"
echo "Target commit created: ${C2_SHORT} Critical feature patch"

# Execute hard reset
git reset --hard HEAD~1 >/dev/null
echo "Executed: git reset --hard HEAD~1"

# The reset commit is now on no branch, so only the reflog can reach it
expect_eq "reset commit is on no branch" "$(git branch --contains "${C2_FULL}")" ""

# Plain git fsck does NOT report C2 because reflogs are reachability roots
FSCK_COUNT="$(git fsck 2>/dev/null | wc -l | tr -d ' ')"
expect_eq "plain git fsck output lines" "${FSCK_COUNT}" "0"
echo "Default git fsck reports dangling objects: ${FSCK_COUNT}"

# Ignoring reflogs exposes the same commit as dangling
FSCK_NR="$(git fsck --no-reflogs 2>/dev/null | grep '^dangling commit' || true)"
expect_eq "git fsck --no-reflogs" "${FSCK_NR}" "dangling commit ${C2_FULL}"
echo "git fsck --no-reflogs reports dangling commit: ${C2_SHORT}"

# Inspect reflog: selector, hash, and action all come from Git
expect_eq "HEAD@{1}" "$(git rev-parse "HEAD@{1}")" "${C2_FULL}"
HEAD1_HASH="$(git rev-parse --short "HEAD@{1}")"
HEAD0_ACTION="$(git reflog show --format='%gs' -n 1 HEAD)"
expect_eq "reflog action" "${HEAD0_ACTION}" "reset: moving to HEAD~1"
echo "Reflog scan: HEAD@{1} points to ${HEAD1_HASH} (action: ${HEAD0_ACTION})"

# Rescue commit by creating a branch pointer
git branch recovered-feature "HEAD@{1}"
echo "Recovery command: git branch recovered-feature HEAD@{1}"

expect_eq "recovered-feature" "$(git rev-parse recovered-feature)" "${C2_FULL}"
REC_LOG="$(git log -1 --format="%h %s" recovered-feature)"
echo "Verification: commit recovered-feature is ${REC_LOG}"

FEATURE_CONTENT="$(git show recovered-feature:feature.txt)"
expect_eq "feature.txt content" "${FEATURE_CONTENT}" "Critical kernel patch"
echo "Content verification: feature.txt recovered with '${FEATURE_CONTENT}'"
S1=PASS
echo ""

# ==============================================================================
# Scenario 2: Recover Staged Uncommitted Work (Dangling Blob Rescue)
# ==============================================================================
echo "=== Step 2: Recover Staged Uncommitted Work (Dangling Blob Rescue) ==="

cat << 'EOF' > hotfix_core.c
// Critical lock-free queue hotfix
int lf_queue_push(void *q, void *item) { return 0; }
EOF
HOTFIX_ORIGINAL="$(cat hotfix_core.c)"
echo "Created uncommitted source: hotfix_core.c"

# Staging writes a loose blob object to .git/objects/
git add hotfix_core.c
BLOB_HASH="$(git hash-object hotfix_core.c)"
expect_eq "staged blob id" "$(git ls-files -s hotfix_core.c | awk '{print $2}')" "${BLOB_HASH}"
echo "Staged file: git add hotfix_core.c (blob hash: ${BLOB_HASH})"

# Disaster: uncommitted staged file wiped by reset --hard
git reset --hard HEAD >/dev/null
echo "Executed: git reset --hard HEAD"

[ ! -f hotfix_core.c ] || die "hotfix_core.c survived reset --hard"
expect_eq "index after reset" "$(git ls-files hotfix_core.c)" ""
echo "Working tree check: hotfix_core.c removed from filesystem"

# The blob is still in the object database but nothing references it
git cat-file -e "${BLOB_HASH}" || die "blob ${BLOB_HASH} missing from object store"
[ ! -e ".git/lost-found/other/${BLOB_HASH}" ] || die "lost-found populated before fsck --lost-found"

# Run git fsck --lost-found to extract dangling blobs into .git/lost-found/other/
git fsck --lost-found >/dev/null 2>&1
echo "Executed: git fsck --lost-found"

LOST_BLOB_PATH=".git/lost-found/other/${BLOB_HASH}"
[ -f "${LOST_BLOB_PATH}" ] || die "blob ${BLOB_HASH} not written to lost-found"
echo "Found in .git/lost-found/other/: ${BLOB_HASH}"

BLOB_FIRST_LINE="$(head -n 1 "${LOST_BLOB_PATH}")"
echo "Blob content inspected: ${BLOB_FIRST_LINE}"

# Restore file
cp "${LOST_BLOB_PATH}" hotfix_core.c
echo "Recovery command: cp .git/lost-found/other/${BLOB_HASH} hotfix_core.c"

expect_eq "recovered hotfix_core.c" "$(cat hotfix_core.c)" "${HOTFIX_ORIGINAL}"
echo "Content verification: recovered hotfix_core.c matches staged source"
rm hotfix_core.c
S2=PASS
echo ""

# ==============================================================================
# Scenario 3: Recover Dropped Stash (Multi-Parent Tree Re-Application)
# ==============================================================================
echo "=== Step 3: Recover Dropped Stash (Multi-Parent Tree Re-Application) ==="

export GIT_AUTHOR_DATE="2026-10-25T12:02:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:02:00Z"

# base.txt carries one staged edit and one unstaged edit; staged_patch.txt is a staged new file
echo "Transaction worker staged" >> base.txt
git add base.txt
echo "Transaction worker unstaged" >> base.txt
echo "Staged payload v1" > staged_patch.txt
git add staged_patch.txt
expect_eq "index before stash" "$(git diff --cached --name-only | tr '\n' ' ')" "base.txt staged_patch.txt "
expect_eq "worktree before stash" "$(git diff --name-only)" "base.txt"
echo "Created dirty working tree and staged index changes"

git stash push -m "WIP: uncommitted queue refactor" >/dev/null
echo "Executed: git stash push -m \"WIP: uncommitted queue refactor\""
expect_eq "tree clean after stash" "$(git status --porcelain)" ""

# Drop the stash
git stash drop >/dev/null
echo "Executed: git stash drop"

STASH_COUNT="$(git stash list | wc -l | tr -d ' ')"
expect_eq "stash list length" "${STASH_COUNT}" "0"
echo "Active stashes in repository: ${STASH_COUNT}"

# Find dangling stash commit via git fsck --no-reflogs
echo "Executed: git fsck --no-reflogs"
DANGLING_COMMITS="$(git fsck --no-reflogs 2>/dev/null | grep 'dangling commit' | awk '{print $3}')"

STASH_COMMIT=""
for c in ${DANGLING_COMMITS}; do
    SUBJECT="$(git log -1 --format=%s "${c}")"
    if [ "${SUBJECT}" = "On main: WIP: uncommitted queue refactor" ]; then
        STASH_COMMIT="${c}"
        break
    fi
done
[ -n "${STASH_COMMIT}" ] || die "dropped stash commit not found among dangling commits"

echo "Identified dangling stash commit: ${STASH_COMMIT}"
PARENT_COUNT="$(git rev-list --parents -n 1 "${STASH_COMMIT}" | awk '{print NF - 1}')"
expect_eq "stash parent count" "${PARENT_COUNT}" "2"
expect_eq "stash parent 1" "$(git rev-parse "${STASH_COMMIT}^1")" "$(git rev-parse HEAD)"
INDEX_PARENT_SUBJECT="$(git log -1 --format=%s "${STASH_COMMIT}^2")"
case "${INDEX_PARENT_SUBJECT}" in
    "index on main: "*) ;;
    *) die "stash parent 2 subject: '${INDEX_PARENT_SUBJECT}'" ;;
esac
echo "Commit parents: ${PARENT_COUNT} (parent 1 is HEAD, parent 2 subject starts with 'index on main:')"

# Restore stash with --index so the staged edits come back staged
git stash apply --index "${STASH_COMMIT}" >/dev/null 2>&1
echo "Recovery command: git stash apply --index ${STASH_COMMIT}"

expect_eq "index after apply" "$(git diff --cached --name-only | tr '\n' ' ')" "base.txt staged_patch.txt "
expect_eq "staged edit of base.txt" "$(git diff --cached base.txt | grep '^+Transaction' | tr '\n' ' ')" "+Transaction worker staged "
expect_eq "unstaged edit of base.txt" "$(git diff base.txt | grep '^+Transaction' | tr '\n' ' ')" "+Transaction worker unstaged "
echo "Verification: index staged (base.txt, staged_patch.txt) and working tree modified (base.txt)"

# A stash made with -u carries a third parent holding the untracked files
git reset --hard HEAD >/dev/null
echo "Untracked scratch notes" > untracked_notes.txt
git stash push -u -m "WIP: untracked notes" >/dev/null
[ ! -e untracked_notes.txt ] || die "stash -u left the untracked file in place"
U_PARENTS="$(git rev-list --parents -n 1 "stash@{0}" | awk '{print NF - 1}')"
expect_eq "stash -u parent count" "${U_PARENTS}" "3"
expect_eq "stash -u third parent files" "$(git ls-tree --name-only 'stash@{0}^3')" "untracked_notes.txt"
echo "Stash with -u: ${U_PARENTS} parents (parent 3 holds untracked_notes.txt)"
git stash drop >/dev/null
S3=PASS
echo ""

# ==============================================================================
# Scenario 4: Recover Detached HEAD Commits (Reflog Pointer Re-Attachment)
# ==============================================================================
echo "=== Step 4: Recover Detached HEAD Commits (Reflog Pointer Re-Attachment) ==="

# Reset changes to clean working tree
git reset --hard HEAD >/dev/null

git checkout --detach HEAD >/dev/null 2>&1
[ "$(git rev-parse --abbrev-ref HEAD)" = "HEAD" ] || die "HEAD is not detached"
echo "Checked out detached HEAD"

export GIT_AUTHOR_DATE="2026-10-25T12:03:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:03:00Z"
echo "Experimental algorithm v2" > experiment.txt
git add experiment.txt
git commit -m "Detached experimental work" -q
DETACHED_FULL="$(git rev-parse HEAD)"
DETACHED_SHORT="$(git rev-parse --short HEAD)"
echo "Created experimental commit: ${DETACHED_SHORT} Detached experimental work"

# Return to main, leaving commit behind
git checkout main >/dev/null 2>&1
echo "Checked out branch main: HEAD moved away from detached commit"
expect_eq "detached commit is on no branch" "$(git branch --contains "${DETACHED_FULL}")" ""

expect_eq "HEAD@{1}" "$(git rev-parse "HEAD@{1}")" "${DETACHED_FULL}"
HEAD1_DETACHED="$(git rev-parse --short "HEAD@{1}")"
HEAD0_ACTION="$(git reflog show --format='%gs' -n 1 HEAD)"
expect_eq "reflog action" "${HEAD0_ACTION}" "checkout: moving from ${DETACHED_FULL} to main"
echo "Reflog scan: HEAD@{1} points to ${HEAD1_DETACHED} (action: ${HEAD0_ACTION})"

# Rescue detached commit
git branch rescued-experiment "HEAD@{1}"
echo "Recovery command: git branch rescued-experiment HEAD@{1}"

expect_eq "rescued-experiment" "$(git rev-parse rescued-experiment)" "${DETACHED_FULL}"
REC_DET_LOG="$(git log -1 --format="%h %s" rescued-experiment)"
echo "Verification: commit rescued-experiment is ${REC_DET_LOG}"
S4=PASS
echo ""

# ==============================================================================
# Summary (a scenario prints PASS only if every assertion above succeeded)
# ==============================================================================
echo "=== Summary: Recovery Playbook Scenarios Verified ==="
echo "Scenario 1 (Hard Reset Commit): ${S1}"
echo "Scenario 2 (Staged Uncommitted Blob): ${S2}"
echo "Scenario 3 (Dropped Stash Commit): ${S3}"
echo "Scenario 4 (Detached HEAD Commits): ${S4}"
