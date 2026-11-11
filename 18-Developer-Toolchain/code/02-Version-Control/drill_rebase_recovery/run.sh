#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
TMPDIR_REPO="${WORK_DIR}/drill"
REAL_REPO="${WORK_DIR}/real"
trap 'rm -rf "${WORK_DIR}"' EXIT
mkdir "${TMPDIR_REPO}" "${REAL_REPO}"

# Assertions write to stderr only, so the golden stdout is untouched and a broken mechanism fails the run.
fail() { echo "ASSERT FAILED: $*" >&2; exit 1; }
expect_eq() { [ "$2" = "$3" ] || fail "$1: got '$2', want '$3'"; }

export GIT_AUTHOR_NAME="Distinguished Engineer"
export GIT_AUTHOR_EMAIL="engineer@example.com"
export GIT_COMMITTER_NAME="Distinguished Committer"
export GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

cd "${TMPDIR_REPO}"
git init -b main -q

# --- 1. BASELINE REPOSITORY SETUP ---
cat <<'EOF' > types.h
#ifndef TYPES_H
#define TYPES_H
#include <stdint.h>
#define RING_BUFFER_CAPACITY 1024
typedef struct {
    uint64_t order_id;
    uint32_t qty;
    uint32_t price;
} Order;
#endif
EOF

cat <<'EOF' > engine.c
#include "types.h"
int process_packet(const void *buf, unsigned long len) {
    (void)buf;
    return (int)len;
}
EOF

git add types.h engine.c
GIT_AUTHOR_DATE="2026-11-08T12:00:00Z" GIT_COMMITTER_DATE="2026-11-08T12:00:00Z" \
    git commit -m "feat(core): initial engine and ring buffer primitives" -q
C1=$(git rev-parse HEAD)

# Main branch update (c2)
cat <<'EOF' > types.h
#ifndef TYPES_H
#define TYPES_H
#include <stdint.h>
#define RING_BUFFER_CAPACITY 65536
typedef struct {
    uint64_t order_id;
    uint32_t qty;
    uint32_t price;
    uint64_t timestamp_ns;
} Order;
#endif
EOF

git add types.h
GIT_AUTHOR_DATE="2026-11-08T12:01:00Z" GIT_COMMITTER_DATE="2026-11-08T12:01:00Z" \
    git commit -m "feat(core): expand ring buffer capacity to 64k and add hardware timestamps" -q
C2=$(git rev-parse HEAD)

# --- 2. PRE-REBASE STACKED BRANCHES OFF C1 ---
git checkout -b feature/feed-handler "$C1" -q
cat <<'EOF' > feed.c
#include "types.h"
int parse_itch_message(const uint8_t *msg) {
    return msg ? 1 : 0;
}
EOF
git add feed.c
GIT_AUTHOR_DATE="2026-11-08T12:02:00Z" GIT_COMMITTER_DATE="2026-11-08T12:02:00Z" \
    git commit -m "feat(feed): parse itch market data feed" -q
S1=$(git rev-parse HEAD)

git checkout -b feature/order-book "$S1" -q
cat <<'EOF' > book.c
#include "types.h"
int match_limit_order(Order *order) {
    return order ? (int)order->qty : 0;
}
EOF
git add book.c
GIT_AUTHOR_DATE="2026-11-08T12:03:00Z" GIT_COMMITTER_DATE="2026-11-08T12:03:00Z" \
    git commit -m "feat(book): implement L2 order book matching engine" -q
S2=$(git rev-parse HEAD)

git checkout -b feature/risk-limits "$S2" -q
cat <<'EOF' > risk.c
#include "types.h"
int validate_risk_limits(const Order *order) {
    if (!order) return -1;
    if (order->qty > 10000) return -2;
    return 0;
}
EOF
git add risk.c
GIT_AUTHOR_DATE="2026-11-08T12:04:00Z" GIT_COMMITTER_DATE="2026-11-08T12:04:00Z" \
    git commit -m "feat(risk): add pre-trade capital limit checks" -q
S3=$(git rev-parse HEAD)

# --- 3. REBASED WORK AND DANGLING TREE CREATION ---
# Rebase feed on C2
git checkout "$C2" -q
cat <<'EOF' > feed.c
#include "types.h"
int parse_itch_message(const uint8_t *msg) {
    return msg ? 1 : 0;
}
EOF
git add feed.c
GIT_AUTHOR_DATE="2026-11-08T12:02:00Z" GIT_COMMITTER_DATE="2026-11-08T12:10:00Z" \
    git commit -m "feat(feed): parse itch market data feed" -q
R1=$(git rev-parse HEAD)

# Rebase book on R1
cat <<'EOF' > book.c
#include "types.h"
int match_limit_order(Order *order) {
    return order ? (int)order->qty : 0;
}
EOF
git add book.c
GIT_AUTHOR_DATE="2026-11-08T12:03:00Z" GIT_COMMITTER_DATE="2026-11-08T12:11:00Z" \
    git commit -m "feat(book): implement L2 order book matching engine" -q
R2=$(git rev-parse HEAD)

# Staged conflict resolution / adaptation of risk.c on top of R2
cat <<'EOF' > risk.c
#include "types.h"
int validate_risk_limits(const Order *order) {
    if (!order) return -1;
    if (order->qty > 10000) return -2;
    if (order->timestamp_ns == 0) return -3;
    return 0;
}
EOF
git add risk.c
DANGLING_TREE=$(git write-tree)

# --- 4. CATASTROPHIC ABORT AND REFLOG EXPIRATION ---
git checkout main -q
git reset --hard main -q
git reflog expire --expire=now --all

# Reset branch heads to stale pre-rebase positions
git update-ref refs/heads/feature/feed-handler "$S1"
git update-ref refs/heads/feature/order-book "$S2"
git update-ref refs/heads/feature/risk-limits "$S3"

# The rebased work must be invisible to every ref and reflog before recovery.
expect_eq "reflog empty after expire" "$(git reflog show HEAD | wc -l | tr -d ' ')" "0"
expect_eq "R2 unreachable from any ref" "$(git branch -a --contains "$R2")" ""
expect_eq "R1 unreachable from any ref" "$(git branch -a --contains "$R1")" ""

echo "=== 1. OBSERVED SYMPTOM: ABORTED REBASE AND EXPIRED REFLOGS ==="
echo "Active branch: $(git branch --show-current) at $(git rev-parse --short HEAD)"
echo "Working tree status: $(git status --porcelain | wc -l | tr -d ' ') modified files"
echo "Reflog entry count: $(git reflog show HEAD | wc -l | tr -d ' ')"
echo "--- Current Branch References ---"
git for-each-ref --format="%(refname:short) -> %(objectname:short) %(subject)" refs/heads/
echo "--- Commit Graph Before Recovery ---"
git log --graph --oneline --decorate --all

echo ""
echo "=== 2. LOW-LEVEL REPOSITORY AUDIT VIA FSCK ==="
git fsck --lost-found --unreachable 2>&1 | sort

echo ""
echo "=== 3. OBJECT TRIAGE AND LINEAGE IDENTIFICATION ==="
DANGLING_COMMIT=$(git fsck --lost-found 2>/dev/null | awk '/dangling commit/ {print $3}')
expect_eq "exactly one dangling commit" "$(git fsck --lost-found 2>/dev/null | grep -c '^dangling commit')" "1"
expect_eq "dangling commit is R2" "${DANGLING_COMMIT}" "$R2"
expect_eq "exactly one dangling tree" "$(git fsck --lost-found 2>/dev/null | grep -c '^dangling tree')" "1"
expect_eq "dangling tree is the written tree" "$(git fsck --lost-found 2>/dev/null | awk '/^dangling tree/ {print $3}')" "${DANGLING_TREE}"
echo "Identified dangling commit tip: ${DANGLING_COMMIT}"
echo "--- Commit Object Details ---"
git cat-file -p "${DANGLING_COMMIT}"

PARENT_COMMIT=$(git cat-file -p "${DANGLING_COMMIT}" | awk '/parent/ {print $2}')
expect_eq "parent of R2 is R1" "${PARENT_COMMIT}" "$R1"
expect_eq "parent of R1 is main" "$(git rev-parse "${PARENT_COMMIT}^")" "$C2"
echo ""
echo "Identified parent commit: ${PARENT_COMMIT}"
echo "--- Parent Commit Details ---"
git cat-file -p "${PARENT_COMMIT}"

echo ""
echo "Identified dangling tree: ${DANGLING_TREE}"
echo "--- Dangling Tree File Manifest ---"
git ls-tree -r "${DANGLING_TREE}"
echo "--- Diff between Dangling Commit Tip and Dangling Tree ---"
git diff "${DANGLING_COMMIT}" "${DANGLING_TREE}"

echo ""
echo "=== 4. PLUMBING SYNTHESIS: MINTING COMMIT FROM DANGLING TREE ==="
export GIT_AUTHOR_DATE="2026-11-08T12:04:00Z"
export GIT_COMMITTER_DATE="2026-11-08T12:12:00Z"
RECOVERED_RISK_COMMIT=$(echo "feat(risk): add pre-trade capital limit checks" | git commit-tree "${DANGLING_TREE}" -p "${DANGLING_COMMIT}")
echo "Synthesized commit for feature/risk-limits: ${RECOVERED_RISK_COMMIT}"
echo "--- Synthesized Commit Object ---"
git cat-file -p "${RECOVERED_RISK_COMMIT}"

echo ""
echo "=== 5. ATOMIC REFERENCE RESTORATION VIA UPDATE-REF ==="
git update-ref refs/heads/feature/feed-handler "${PARENT_COMMIT}"
git update-ref refs/heads/feature/order-book "${DANGLING_COMMIT}"
git update-ref refs/heads/feature/risk-limits "${RECOVERED_RISK_COMMIT}"
echo "Successfully updated refs/heads/feature/feed-handler -> ${PARENT_COMMIT}"
echo "Successfully updated refs/heads/feature/order-book   -> ${DANGLING_COMMIT}"
echo "Successfully updated refs/heads/feature/risk-limits  -> ${RECOVERED_RISK_COMMIT}"

echo ""
echo "=== 6. VERIFICATION OF RESTORED STACKED GRAPH AND COMPILATION ==="
git checkout feature/risk-limits -q
expect_eq "recovered tip tree is the dangling tree" "$(git rev-parse 'feature/risk-limits^{tree}')" "${DANGLING_TREE}"
expect_eq "stack sits on main" "$(git merge-base main feature/risk-limits)" "$C2"
expect_eq "three commits above main" "$(git rev-list --count main..feature/risk-limits)" "3"
expect_eq "R1 is an ancestor of the tip" "$(git merge-base --is-ancestor "$R1" feature/risk-limits && echo yes)" "yes"
git show feature/risk-limits:risk.c | grep -q 'timestamp_ns' || fail "recovered risk.c lacks the timestamp_ns check"
echo "--- Reconstructed Linear Commit Graph ---"
git log --graph --oneline --decorate --all

echo "--- Cumulative Diff Against Main ---"
git diff main..feature/risk-limits --stat

"${CC:-cc}" -Wall -Werror -c engine.c feed.c book.c risk.c
echo "Build verification: PASS (all modules compiled with -Wall -Werror)"

echo ""
echo "=== 7. REAL SEQUENCER CROSS-CHECK: git rebase --update-refs, conflict stop, abort, reflog expire ==="
# Sections 1-6 fake the end state with plumbing. This section drives the real sequencer in a second repository
# so the .git/rebase-merge, abort, reflog, and fsck claims are observed instead of assumed.
unset GIT_AUTHOR_DATE GIT_COMMITTER_DATE
export GIT_AUTHOR_DATE="2026-11-08T13:00:00Z" GIT_COMMITTER_DATE="2026-11-08T13:00:00Z"
cd "${REAL_REPO}"
git init -b main -q
printf '#define RING_BUFFER_CAPACITY 1024\n' > types.h
git add types.h
git commit -m "base" -q
BASE=$(git rev-parse HEAD)

git checkout -b feed -q
printf 'int feed(void) { return 1; }\n' > feed.c
git add feed.c
git commit -m "feed: add feed.c" -q
git checkout -b book -q
printf '#define RING_BUFFER_CAPACITY 2048\n' > types.h
git add types.h
git commit -m "book: raise capacity to 2048" -q
OLD_FEED=$(git rev-parse feed)
OLD_BOOK=$(git rev-parse book)

git checkout main -q
printf '#define RING_BUFFER_CAPACITY 65536\n' > types.h
git add types.h
git commit -m "main: raise capacity to 64k" -q
NEW_MAIN=$(git rev-parse HEAD)

git checkout book -q
if git rebase --update-refs main >/dev/null 2>&1; then
    fail "the rebase finished; the types.h edits were meant to conflict"
fi
R_FEED=$(git rev-parse HEAD)
RB=$(git rev-parse --git-path rebase-merge)
echo "--- Sequencer state while stopped on the conflict ---"
echo "rebase-merge directory: $([ -d "$RB" ] && echo present || echo absent)"
for f in git-rebase-todo done head-name onto orig-head stopped-sha update-refs; do
    printf '%s: %s\n' "$f" "$([ -e "$RB/$f" ] && echo present || echo absent)"
done
echo "head-name: $(cat "$RB/head-name")"
echo "update-refs queues feed: $(grep -q 'refs/heads/feed' "$RB/update-refs" && echo yes || echo no)"
echo "onto is main: $([ "$(cat "$RB/onto")" = "$NEW_MAIN" ] && echo yes || echo no)"
echo "HEAD is detached: $([ -z "$(git branch --show-current)" ] && echo yes || echo no)"
echo "rebased feed commit sits on main: $([ "$(git rev-parse "${R_FEED}^")" = "$NEW_MAIN" ] && echo yes || echo no)"
echo "conflicted paths: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"
expect_eq "orig-head is the pre-rebase book tip" "$(cat "$RB/orig-head")" "$OLD_BOOK"
expect_eq "no branch holds the rebased feed commit" "$(git branch -a --contains "${R_FEED}" | grep -vc '(no branch\|HEAD detached' || true)" "0"

git rebase --abort
echo "--- After git rebase --abort ---"
echo "rebase-merge directory: $([ -d "$RB" ] && echo present || echo absent)"
echo "book restored to pre-rebase tip: $([ "$(git rev-parse book)" = "$OLD_BOOK" ] && echo yes || echo no)"
echo "feed restored to pre-rebase tip: $([ "$(git rev-parse feed)" = "$OLD_FEED" ] && echo yes || echo no)"
echo "ORIG_HEAD survives the abort: $([ "$(cat "$(git rev-parse --git-path ORIG_HEAD)")" = "$OLD_BOOK" ] && echo yes || echo no)"
echo "HEAD reflog records the pick: $(git reflog show HEAD | grep -c 'rebase (pick)' || true) pick entries"
echo "HEAD reflog records the abort: $(git reflog show HEAD | grep -c 'rebase (abort): returning to' || true) abort entry"
expect_eq "abort leaves the rebased feed commit in the object store" "$(git cat-file -t "${R_FEED}")" "commit"

git reflog expire --expire=now --all
echo "--- After git reflog expire --expire=now --all ---"
echo "HEAD reflog entries: $(git reflog show HEAD | wc -l | tr -d ' ')"
echo "HEAD log file bytes: $(wc -c < "$(git rev-parse --git-path logs/HEAD)" | tr -d ' ')"
DANGLING_REAL=$(git fsck --lost-found 2>/dev/null | awk '/^dangling commit/ {print $3}')
echo "dangling commit count: $(printf '%s\n' "${DANGLING_REAL}" | grep -c . || true)"
echo "dangling commit is the rebased feed commit: $([ "${DANGLING_REAL}" = "${R_FEED}" ] && echo yes || echo no)"
CONFLICT_TREES=0
for t in $(git fsck --lost-found 2>/dev/null | awk '/^dangling tree/ {print $3}'); do
    if git cat-file -p "$t:types.h" 2>/dev/null | grep -q '^<<<<<<<'; then CONFLICT_TREES=$((CONFLICT_TREES + 1)); fi
done
echo "dangling trees holding conflict markers in types.h: ${CONFLICT_TREES}"
git update-ref refs/heads/feed "${R_FEED}"
echo "--- After git update-ref refs/heads/feed <dangling commit> ---"
echo "feed is based on main: $([ "$(git merge-base main feed)" = "$NEW_MAIN" ] && echo yes || echo no)"
echo "feed carries only its own commit: $(git rev-list --count main..feed) commit"
