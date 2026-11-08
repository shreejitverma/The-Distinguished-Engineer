#!/usr/bin/env bash
set -euo pipefail
# Needs git >= 2.47 (multi-pack-index write --incremental), a C compiler, and python3.
# Pack names and sizes depend on the git and zlib versions: expected/ was recorded with git 2.55.0.

GIT_VERSION="$(git version | awk '{print $3}')"
if ! python3 -I -c 'import sys; v = tuple(int(x) for x in sys.argv[1].split(".")[:3]); sys.exit(0 if v >= (2, 47, 0) else 1)' "${GIT_VERSION}"; then
    echo "git ${GIT_VERSION} is too old: this lab needs git >= 2.47" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_REPO="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_REPO}"' EXIT

export GIT_AUTHOR_NAME="Distinguished Engineer"
export GIT_AUTHOR_EMAIL="engineer@example.com"
export GIT_COMMITTER_NAME="Distinguished Committer"
export GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

cd "${TMPDIR_REPO}"

# --- HELPERS: every check prints one line and aborts the lab when it fails ---
check() {
    local desc="$1"
    shift
    if "$@"; then
        echo "  [check] ${desc}: ok"
    else
        echo "  [check] ${desc}: FAILED"
        exit 1
    fi
}

# Silent precondition: aborts the lab with a message on stderr.
require() {
    local desc="$1"
    shift
    if ! "$@"; then
        echo "precondition failed: ${desc}" >&2
        exit 1
    fi
}

file_size() { wc -c < "$1" | tr -d ' '; }

idx_header_ok() {
    python3 -I -c 'import sys; sys.exit(0 if open(sys.argv[1], "rb").read(8) == b"\xfftOc\x00\x00\x00\x02" else 1)' "$1"
}

# The 20 bytes before the index checksum must equal the trailing checksum of the pack.
idx_trailer_matches_pack() {
    python3 -I -c 'import sys; i = open(sys.argv[1], "rb").read(); p = open(sys.argv[2], "rb").read(); sys.exit(0 if i[-40:-20] == p[-20:] else 1)' "$1" "$2"
}

# MIDX header: 4-byte signature, 1-byte version (1), 1-byte hash version, chunk count, base count, pack count.
midx_header_ok() {
    python3 -I -c 'import sys; d = open(sys.argv[1], "rb").read(12); sys.exit(0 if len(d) == 12 and d[:4] == b"MIDX" and d[4] == 1 else 1)' "$1"
}

packs_digest() {
    python3 -I -c '
import glob, hashlib, sys
h = hashlib.sha1()
for f in sorted(glob.glob(sys.argv[1] + "/*.pack")):
    h.update(open(f, "rb").read())
print(h.hexdigest())
' "$1"
}

# --- 1. BASELINE REPOSITORY SETUP ---
git init -b main -q

# Base commit: initial trading engine core v1.0
cat <<'EOF' > engine.c
/* Trading Engine Core v1.0 */
#include <stdint.h>

void run_engine(void) {
    /* baseline single-threaded event loop */
}
EOF

cat <<'EOF' > ring_buffer.h
#ifndef RING_BUFFER_H
#define RING_BUFFER_H
#include <stdint.h>
#define RING_BUFFER_CAPACITY 1024
typedef struct {
    uint64_t seq;
    uint32_t price;
    uint32_t qty;
} OrderEvent;
#endif
EOF

git add engine.c ring_buffer.h
GIT_AUTHOR_DATE="2026-11-08T12:00:00Z" GIT_COMMITTER_DATE="2026-11-08T12:00:00Z" \
    git commit -m "feat(engine): initial trading core v1.0" -q
git tag v1.0.0

# Pack base objects into initial packfile
git -c pack.threads=1 repack -ad -q

PACK_DIR=".git/objects/pack"
BASE_PACK="$(cd "${PACK_DIR}" && ls pack-*.pack | head -n 1)"

# Second commit: production release candidate v2.4.0-rc1
cat <<'EOF' > engine.c
/* Trading Engine Core v2.4-rc1 */
#include <stdint.h>
#include "ring_buffer.h"

void run_engine(void) {
    /* zero-copy ring-buffer dispatch */
}
EOF

cat <<'EOF' > order_book.c
/* High-Throughput Limit Order Book */
#include "ring_buffer.h"

int match_orders(const OrderEvent *ev) {
    return (ev != 0) ? (int)ev->qty : 0;
}
EOF

git add engine.c order_book.c
GIT_AUTHOR_DATE="2026-11-08T12:30:00Z" GIT_COMMITTER_DATE="2026-11-08T12:30:00Z" \
    git commit -m "fix(engine): zero-copy order matching for v2.4-rc1" -q
git tag v2.4.0-rc1

# Pack new objects into incremental packfile and prune loose objects
git -c pack.threads=1 repack -d -q
git prune

# Generate incremental multi-pack-index chain
git multi-pack-index --no-progress write --incremental

RECENT_PACK="$(cd "${PACK_DIR}" && ls pack-*.pack | grep -v "${BASE_PACK}")"
RECENT_IDX="${RECENT_PACK%.pack}.idx"
MIDX_CHAIN_FILE="${PACK_DIR}/multi-pack-index.d/multi-pack-index-chain"
LAYER_HASH="$(cat "${MIDX_CHAIN_FILE}")"
MIDX_LAYER_FILE="${PACK_DIR}/multi-pack-index.d/multi-pack-index-${LAYER_HASH}.midx"

# The corruption below must hit the live layer, so prove that exactly one valid layer exists first.
require "one valid MIDX layer before corruption" test "$(wc -l < "${MIDX_CHAIN_FILE}" | tr -d ' ')" -eq 1
require "layer header valid before corruption" midx_header_ok "${MIDX_LAYER_FILE}"
PACKS_BEFORE="$(packs_digest "${PACK_DIR}")"

# --- 2. INTRODUCE PACKFILE INDEX AND MIDX CORRUPTIONS ---
# Simulate power loss or interrupted maintenance during packfile indexing
chmod 644 "${PACK_DIR}/${RECENT_IDX}"
printf '\xff\x74\x4f\x63\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00\x01' > "${PACK_DIR}/${RECENT_IDX}"
chmod 444 "${PACK_DIR}/${RECENT_IDX}"

chmod 644 "${MIDX_LAYER_FILE}"
printf 'MIDX\x01\x01\x00\x00' > "${MIDX_LAYER_FILE}"
chmod 444 "${MIDX_LAYER_FILE}"
touch -t 202611081200.00 "${PACK_DIR}"/*

echo "=== 1. OBSERVED SYMPTOM: FAILED OBJECT READS AND TRAVERSAL ABORT ==="
echo "[Command] git log -1"
set +e
LOG_OUT="$(git log -1 2>&1)"
LOG_STATUS=$?
set -e
echo "${LOG_OUT}"
echo "Exit status: ${LOG_STATUS}"

echo ""
echo "[Command] git checkout v2.4.0-rc1"
set +e
CHECKOUT_OUT="$(git checkout v2.4.0-rc1 2>&1)"
CHECKOUT_STATUS=$?
set -e
echo "${CHECKOUT_OUT}"
echo "Exit status: ${CHECKOUT_STATUS}"

echo ""
echo "[Command] git multi-pack-index verify"
set +e
MIDX_VERIFY_OUT="$(git multi-pack-index --no-progress verify 2>&1)"
MIDX_STATUS=$?
set -e
echo "${MIDX_VERIFY_OUT}"
echo "Exit status: ${MIDX_STATUS}"

echo ""
echo "=== 2. OBJECT STORE INSPECTION AND CORRUPTION ISOLATION ==="
echo "--- Object Database Pack Directory Manifest ---"
python3 -c '
import os
for root, dirs, files in os.walk(".git/objects/pack"):
    for f in sorted(files):
        p = os.path.join(root, f)
        rel = os.path.relpath(p, ".git/objects/pack")
        print(f"  {rel:62s} {os.path.getsize(p):6d} bytes")
'

echo "--- Reference Pointers in .git/refs ---"
git for-each-ref --format="  %(refname:short) -> %(objectname)" refs/

echo "--- Low-Level Consistency Audit (git fsck) ---"
set +e
FSCK_OUT="$(git fsck --no-progress 2>&1)"
FSCK_STATUS=$?
set -e
echo "${FSCK_OUT}"
echo "Exit status: ${FSCK_STATUS}"

echo ""
echo "=== 3. REMEDIATION STEP 1: PURGE CORRUPTED MIDX METADATA ==="
echo "Removing broken multi-pack-index chain directory..."
rm -rf "${PACK_DIR}/multi-pack-index.d"
rm -f "${PACK_DIR}/multi-pack-index"
echo "MIDX metadata purged successfully."
check "multi-pack-index.d is gone" test ! -e "${PACK_DIR}/multi-pack-index.d"
check "monolithic multi-pack-index is absent" test ! -e "${PACK_DIR}/multi-pack-index"

echo ""
echo "=== 4. REMEDIATION STEP 2: REGENERATE PACKFILE INDEXES VIA GIT INDEX-PACK ==="
echo "Removing truncated index: ${RECENT_IDX}..."
rm -f "${PACK_DIR}/${RECENT_IDX}"

echo "Executing git index-pack on raw packfile: ${RECENT_PACK}..."
INDEX_PACK_OUT="$(git index-pack "${PACK_DIR}/${RECENT_PACK}")"
echo "Regenerated pack checksum: ${INDEX_PACK_OUT}"

echo "Verifying restored index file size:"
python3 -c "
import os
p = '${PACK_DIR}/${RECENT_IDX}'
print(f'  {os.path.basename(p):62s} {os.path.getsize(p):6d} bytes')
"
NEW_IDX="${PACK_DIR}/${RECENT_IDX}"
NEW_OBJECTS="$(git show-index < "${NEW_IDX}" 2>/dev/null | wc -l | tr -d ' ')" || NEW_OBJECTS=0
check "index-pack printed the checksum that names the pack" test "pack-${INDEX_PACK_OUT}.pack" = "${RECENT_PACK}"
check "index has the v2 magic and version" idx_header_ok "${NEW_IDX}"
check "index lists all ${NEW_OBJECTS} objects" test "${NEW_OBJECTS}" -eq 4
check "index size is 8 + 1024 + 28 * objects + 40" test "$(file_size "${NEW_IDX}")" -eq $((8 + 1024 + 28 * NEW_OBJECTS + 40))
check "index trailer carries the pack checksum" idx_trailer_matches_pack "${NEW_IDX}" "${PACK_DIR}/${RECENT_PACK}"

echo ""
echo "=== 5. REMEDIATION STEP 3: VERIFY PACKFILE INTEGRITY VIA GIT VERIFY-PACK ==="
VERIFY_OUT="$(git verify-pack -v .git/objects/pack/pack-*.pack)"
echo "${VERIFY_OUT}"
check "verify-pack reports both packs ok" test "$(grep -c ': ok$' <<< "${VERIFY_OUT}")" -eq 2

echo ""
echo "=== 6. REMEDIATION STEP 4: RECONSTRUCT AND VERIFY MULTI-PACK-INDEX ==="
echo "Rebuilding incremental multi-pack-index..."
git multi-pack-index --no-progress write --incremental
echo "Verifying rebuilt multi-pack-index..."
git multi-pack-index --no-progress verify
# verify exits 0 even when no MIDX loads, so prove the rebuild from the files themselves.
REBUILT_LAYER="$(cat "${MIDX_CHAIN_FILE}" 2>/dev/null)" || REBUILT_LAYER=missing
REBUILT_FILE="${PACK_DIR}/multi-pack-index.d/multi-pack-index-${REBUILT_LAYER}.midx"
check "chain file exists" test -f "${MIDX_CHAIN_FILE}"
check "chain lists exactly one layer" test "$(wc -l < "${MIDX_CHAIN_FILE}" | tr -d ' ')" -eq 1
check "layer file exists" test -f "${REBUILT_FILE}"
check "layer has a valid MIDX header" midx_header_ok "${REBUILT_FILE}"
check "layer names the base pack" grep -aqF "${BASE_PACK%.pack}.idx" "${REBUILT_FILE}"
check "layer names the rescued pack" grep -aqF "${RECENT_IDX}" "${REBUILT_FILE}"

echo ""
echo "=== 7. POST-RESCUE AUDIT AND DEPLOYMENT VALIDATION ==="
echo "--- Repository Consistency Check (git fsck --full) ---"
set +e
FINAL_FSCK_OUT="$(git fsck --full --no-progress 2>&1)"
FINAL_FSCK_STATUS=$?
set -e
echo "git fsck exit status: ${FINAL_FSCK_STATUS}"
check "fsck exits 0" test "${FINAL_FSCK_STATUS}" -eq 0
check "fsck prints nothing" test -z "${FINAL_FSCK_OUT}"
check "pack payloads are byte-identical to before the corruption" test "$(packs_digest "${PACK_DIR}")" = "${PACKS_BEFORE}"

echo "--- Commit Log Verification ---"
git log -2 --oneline

echo "--- Working Tree Checkout for Release Deployment ---"
git checkout -q v2.4.0-rc1
echo "HEAD detached at: $(git rev-parse --short HEAD) (tag: v2.4.0-rc1)"
git status --short
check "HEAD is the tagged commit" test "$(git rev-parse HEAD)" = "$(git rev-parse 'v2.4.0-rc1^{commit}')"
check "working tree is clean" test -z "$(git status --short)"

echo "--- Production Build Gate Verification ---"
"${CC:-cc}" -Wall -Werror -c engine.c order_book.c
echo "Build verification: PASS (engine.o and order_book.o compiled cleanly)"
