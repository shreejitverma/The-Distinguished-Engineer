#!/usr/bin/env bash
set -euo pipefail

TMPDIR_LAB="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_LAB}"' EXIT

export GIT_AUTHOR_NAME="Staff Engineer"
export GIT_AUTHOR_EMAIL="engineer@example.com"
export GIT_COMMITTER_NAME="Staff Engineer"
export GIT_COMMITTER_EMAIL="engineer@example.com"
export GIT_AUTHOR_DATE="2026-11-08T10:00:00Z"
export GIT_COMMITTER_DATE="2026-11-08T10:00:00Z"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

ORIGIN="${TMPDIR_LAB}/origin.git"
WORK="${TMPDIR_LAB}/work"
MIRROR="${TMPDIR_LAB}/mirror.git"

# 1. Initialize upstream bare repository and worktree clone
git init --bare -b main -q "${ORIGIN}"
git clone -q "${ORIGIN}" "${WORK}" 2>/dev/null

# 2. Seed historical commits
cd "${WORK}"
mkdir -p src
cat << 'EOF' > src/main.py
def run():
    print("service initialized")
EOF
cat << 'EOF' > README.md
# Payment Gateway Infrastructure
EOF
git add .
git commit -q -m "feat: initialize payment gateway service"

# Commit 2: Leaked secrets across files
mkdir -p keys config
cat << 'EOF' > keys/service_account.pem
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA0m4pQ...PRIVATE_KEY_PAYLOAD...==
-----END RSA PRIVATE KEY-----
EOF
cat << 'EOF' > config/credentials.env
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
EOF
cat << 'EOF' > src/payment.py
STRIPE_SECRET_KEY = "sk_live_EXAMPLE-NOT-A-REAL-KEY"
EOF

GIT_AUTHOR_NAME="Leaky Contractor" \
GIT_AUTHOR_EMAIL="leaky@external-vendor.internal" \
GIT_AUTHOR_DATE="2026-11-08T11:00:00Z" \
GIT_COMMITTER_DATE="2026-11-08T11:00:00Z" \
git add .
GIT_AUTHOR_NAME="Leaky Contractor" \
GIT_AUTHOR_EMAIL="leaky@external-vendor.internal" \
GIT_AUTHOR_DATE="2026-11-08T11:00:00Z" \
GIT_COMMITTER_DATE="2026-11-08T11:00:00Z" \
git commit -q -m "feat: integrate payment credentials and infra keys"

C2_HASH="$(git rev-parse HEAD)"
PEM_BLOB="$(git rev-parse HEAD:keys/service_account.pem)"

# Commit 3: Work commit referencing commit 2 hash in message
cat << 'EOF' > src/worker.py
def process_queue():
    pass
EOF
git add src/worker.py
GIT_AUTHOR_DATE="2026-11-08T12:00:00Z" \
GIT_COMMITTER_DATE="2026-11-08T12:00:00Z" \
git commit -q -m "fix: add worker queue following credentials commit ${C2_HASH}"
git tag -a v1.0.0 -m "Release v1.0.0"

git push -q origin main --tags

echo "=== Initial Repository State ==="
echo "Commit 2 hash: ${C2_HASH}"
echo "Secret PEM blob hash: ${PEM_BLOB}"
git log --oneline --format="%h %an <%ae> %s"

# Positive control: the strings the checks look for really are in the pre-rewrite history
for needle in wJalrXUtnFEMI sk_live_EXAMPLE-NOT-A-REAL-KEY; do
    # shellcheck disable=SC2046
    git grep -q -I -e "${needle}" $(git rev-list --all) || { echo "Control FAILED: ${needle} not in seeded history"; exit 1; }
done

# 3. Create fresh mirror clone for safe incident remediation
git clone -q --mirror "${ORIGIN}" "${MIRROR}"
cd "${MIRROR}"

# 4. Analyze repository history
git-filter-repo --analyze >/dev/null 2>&1
echo "=== Top Blobs from History Analysis ==="
head -n 5 filter-repo/analysis/blob-shas-and-paths.txt

# 5. Define replacement rules and mailmap
cat << 'EOF' > "${TMPDIR_LAB}/replace.txt"
wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY==>[REDACTED-AWS-KEY]
sk_live_EXAMPLE-NOT-A-REAL-KEY==>[REDACTED-STRIPE-KEY]
EOF

cat << 'EOF' > "${TMPDIR_LAB}/mailmap.txt"
Staff Engineer <engineer@example.com> <leaky@external-vendor.internal>
EOF

# 6. Execute history rewriting
# Scrub timing variations from filter-repo output for deterministic verification
git-filter-repo \
  --invert-paths --path keys/service_account.pem \
  --replace-text "${TMPDIR_LAB}/replace.txt" \
  --mailmap "${TMPDIR_LAB}/mailmap.txt" 2>&1 \
  | sed -E \
    -e 's/in [0-9]+\.[0-9]+ seconds/in X.XX seconds/g' \
    -e 's/after [0-9]+\.[0-9]+ seconds/after X.XX seconds/g' \
    -e 's#was .*/origin.git#was <ORIGIN_URL>#g'

echo "=== Verification Checks ==="
FAILURES=0
check() {
    # check <label> <pass text> <fail text> <command...>; the command must succeed to pass
    local label="$1" pass_text="$2" fail_text="$3"
    shift 3
    if "$@"; then
        echo "Check ${label} PASSED: ${pass_text}"
    else
        echo "Check ${label} FAILED: ${fail_text}"
        FAILURES=$((FAILURES + 1))
    fi
}

# Greps run against captured output, never in a pipeline, so pipefail and SIGPIPE cannot flip a result
OBJECT_LIST="$(git rev-list --objects --all)"
ALL_REVS="$(git rev-list --all)"
# shellcheck disable=SC2086
history_lacks() {
    # git grep exits 1 on "no match" and 2+ on error; only 1 counts as absent
    local rc=0
    [ -n "${ALL_REVS}" ] || return 1
    git grep -q -I -e "$1" ${ALL_REVS} || rc=$?
    [ "${rc}" -eq 1 ]
}
tree_lacks_path() { ! grep -q "$1" <<<"${OBJECT_LIST}"; }
blob_missing() { ! git cat-file -e "$1" 2>/dev/null; }
no_remotes() { [ -z "$(git remote)" ]; }

check A "keys/service_account.pem completely purged from tree objects" \
    "keys/service_account.pem still reachable in revision graph" \
    tree_lacks_path "keys/service_account.pem"

check B "AWS secret key purged and replaced" \
    "AWS secret key string found in history" \
    history_lacks "wJalrXUtnFEMI"

check C "Secret PEM blob purged by post-rewrite repack" \
    "Secret PEM blob still present in object database" \
    blob_missing "${PEM_BLOB}"

check D "Remote 'origin' removed by git-filter-repo to prevent accidental push" \
    "remote still configured: $(git remote)" \
    no_remotes

# Check E: look the rewritten hash up by its old hash; commit-map is sorted by old hash, not by position
NEW_C2_HASH="$(awk -v old="${C2_HASH}" '$1 == old {print $2}' filter-repo/commit-map)"
C3_MSG="$(git log -1 --pretty=%B main)"
echo "Rewritten Commit 2 hash: ${NEW_C2_HASH}"
echo "Rewritten Commit 3 message:"
echo "${C3_MSG}"
message_rewritten() {
    [ -n "${NEW_C2_HASH}" ] && [ "${NEW_C2_HASH}" != "${C2_HASH}" ] \
        && [ "$(git rev-parse main~1)" = "${NEW_C2_HASH}" ] \
        && [[ "${C3_MSG}" == *"${NEW_C2_HASH}"* ]] && [[ "${C3_MSG}" != *"${C2_HASH}"* ]]
}
check E "commit 3 message cites the rewritten hash of commit 2, not the old one" \
    "commit 3 message was not translated to the rewritten commit 2 hash" \
    message_rewritten

# Check F: author identity sanitization across history
echo "Rewritten Commit Authors:"
git log --format="%h %an <%ae> %s" main
no_leaky_identity() {
    local identities
    identities="$(git log --all --format='%an <%ae>%n%cn <%ce>')"
    [ -n "${identities}" ] && ! grep -q 'external-vendor' <<<"${identities}"
}
check F "no author or committer identity from external-vendor.internal remains" \
    "leaky identity still present in commit headers" \
    no_leaky_identity

# Check G: the Stripe key is replaced in place and the credentials file survives
stripe_redacted() {
    history_lacks "sk_live_EXAMPLE-NOT-A-REAL-KEY" \
        && [ "$(git show main:src/payment.py)" = 'STRIPE_SECRET_KEY = "[REDACTED-STRIPE-KEY]"' ]
}
check G "Stripe key replaced in src/payment.py, file kept" \
    "Stripe key not redacted in place" \
    stripe_redacted

credentials_kept() {
    git cat-file -e main:config/credentials.env 2>/dev/null \
        && git show main:config/credentials.env | grep -q '^AWS_SECRET_ACCESS_KEY=\[REDACTED-AWS-KEY\]$' \
        && git show main:config/credentials.env | grep -q '^AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE$'
}
check H "config/credentials.env kept, secret key redacted, access key id untouched" \
    "config/credentials.env missing or not redacted as expected" \
    credentials_kept

# 7. Reconnect remote and force-push sanitized history upstream
git remote add origin "${ORIGIN}"
git push -q --force --mirror origin

# 8. Server-side garbage collection and verification on origin.git
cd "${ORIGIN}"
git reflog expire --expire=now --all
git prune --expire=now
git gc --prune=now -q

if git cat-file -e "${PEM_BLOB}" 2>/dev/null; then
    echo "Server Check FAILED: Secret blob still present on origin server"
    FAILURES=$((FAILURES + 1))
else
    echo "Server Check PASSED: Secret blob completely expunged from origin server"
fi

[ "${FAILURES}" -eq 0 ]
