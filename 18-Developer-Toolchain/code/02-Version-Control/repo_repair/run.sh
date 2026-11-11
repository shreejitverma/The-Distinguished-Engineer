#!/usr/bin/env bash
set -euo pipefail

TMPDIR_REPO="$(mktemp -d)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_REPO}" "${WORK_DIR}"' EXIT
FAILED=0

export GIT_AUTHOR_NAME="Toolchain Author"
export GIT_AUTHOR_EMAIL="author@example.com"
export GIT_COMMITTER_NAME="Toolchain Committer"
export GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_AUTHOR_DATE="2026-10-25T12:00:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:00:00Z"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

cd "${TMPDIR_REPO}"
git init -b main -q

echo "=== Step 1: Auxiliary Cache Repair (Commit-Graph) ==="
echo "int main(void) { return 0; }" > main.c
git add main.c
git commit -m "initial commit" -q
MAIN_BLOB="$(git rev-parse HEAD:main.c)"
git commit-graph write --reachable --no-progress

# Corrupt commit-graph header magic
CG_FILE=".git/objects/info/commit-graph"
chmod +w "${CG_FILE}"
printf "\x00\x00\x00\x00" | dd of="${CG_FILE}" bs=1 count=4 conv=notrunc 2>/dev/null

echo -n "Pre-repair commit-graph verify: "
OUTPUT_CG="$(git commit-graph verify --no-progress 2>&1 || true)"
if echo "${OUTPUT_CG}" | grep -q "commit-graph signature"; then
    echo "CORRUPT (signature mismatch detected)"
else
    echo "UNEXPECTED PASS: ${OUTPUT_CG}"
    FAILED=1
fi

# Remediate: remove broken cache and regenerate
rm -f "${CG_FILE}"
git commit-graph write --reachable --no-progress
echo -n "Post-repair commit-graph verify: "
if git commit-graph verify --no-progress 2>&1; then
    echo "VALID (exit 0)"
else
    echo "FAILED"
    FAILED=1
fi

echo ""
echo "=== Step 2: Index File Reconstruction (.git/index) ==="
# Corrupt index magic
printf "\x00\x00\x00\x00" | dd of=".git/index" bs=1 count=4 conv=notrunc 2>/dev/null
echo -n "Pre-repair index status check: "
OUTPUT_IDX="$(git status 2>&1 || true)"
if echo "${OUTPUT_IDX}" | grep -q "fatal: index file corrupt"; then
    echo "CORRUPT (fatal: index file corrupt detected)"
else
    echo "UNEXPECTED STATUS: ${OUTPUT_IDX}"
    FAILED=1
fi

# Remediate: delete corrupted index and rebuild from HEAD tree
rm -f .git/index
git read-tree HEAD
echo -n "Post-repair working tree status: "
STATUS_OUT="$(git status --porcelain)"
if [ -z "${STATUS_OUT}" ]; then
    echo "CLEAN (matching HEAD)"
else
    echo "DIRTY: ${STATUS_OUT}"
    FAILED=1
fi

echo ""
echo "=== Step 3: Zero-Byte Loose Object Recovery ==="
echo "void engine_run(void) {}" > engine.c
git add engine.c
export GIT_AUTHOR_DATE="2026-10-25T12:01:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:01:00Z"
git commit -m "add engine" -q
BLOB_HASH="$(git rev-parse HEAD:engine.c)"
BLOB_PATH=".git/objects/${BLOB_HASH:0:2}/${BLOB_HASH:2}"

# Simulate unclean power loss before object sync
chmod +w "${BLOB_PATH}"
: > "${BLOB_PATH}"

echo -n "Pre-repair fsck loose object check: "
FSCK_PRE="$(git fsck --full --no-dangling --no-progress 2>&1 || true)"
if echo "${FSCK_PRE}" | grep -q "is empty"; then
    echo "EMPTY LOOSE OBJECT DETECTED (${BLOB_HASH})"
else
    echo "UNEXPECTED FSCK: ${FSCK_PRE}"
    FAILED=1
fi

# Remediate: remove zero-byte file and rehash from working tree copy
rm -f "${BLOB_PATH}"
REHASHED="$(git hash-object -w engine.c)"
echo -n "Rehashed blob object: "
if [ "${REHASHED}" = "${BLOB_HASH}" ]; then
    echo "MATCH (${REHASHED})"
else
    echo "MISMATCH: ${REHASHED} != ${BLOB_HASH}"
    FAILED=1
fi

echo -n "Post-repair fsck check: "
if git fsck --full --no-dangling --no-progress >/dev/null 2>&1; then
    echo "VALID (0 errors)"
else
    echo "FAILED"
    FAILED=1
fi

echo ""
echo "=== Step 4: Packfile Index (.idx) Regeneration ==="
# Repack everything into packfile
git repack -a -d -q
PACK_BASE="$(find .git/objects/pack -name 'pack-*.pack' | head -n 1)"
IDX_FILE="${PACK_BASE%.pack}.idx"

# Remove index file
rm -f "${IDX_FILE}"
echo -n "Pre-repair packfile verification: "
OUTPUT_VERIFY="$(git verify-pack "${IDX_FILE}" 2>&1 || true)"
if echo "${OUTPUT_VERIFY}" | grep -q "Cannot open existing pack idx file"; then
    echo "MISSING INDEX DETECTED"
else
    echo "UNEXPECTED VERIFY-PACK: ${OUTPUT_VERIFY}"
    FAILED=1
fi

# Remediate: rebuild .idx from .pack using git index-pack
git index-pack "${PACK_BASE}" >/dev/null
echo -n "Post-repair verify-pack: "
if git verify-pack "${IDX_FILE}" >/dev/null 2>&1; then
    echo "VALID (all packed objects verified)"
else
    echo "FAILED"
    FAILED=1
fi

echo ""
echo "=== Step 5: Damaged Packfile Salvage and Object Reconstruction ==="
# Create a dedicated two-commit branch to test packfile salvage
git checkout -b test-salvage -q
echo "salvage file 1" > s1.txt
git add s1.txt
export GIT_AUTHOR_DATE="2026-10-25T12:02:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:02:00Z"
git commit -m "salvage commit 1" -q

echo "salvage file 2" > s2.txt
git add s2.txt
export GIT_AUTHOR_DATE="2026-10-25T12:03:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:03:00Z"
git commit -m "salvage commit 2" -q

# Repack into one pack and keep a pristine copy outside the repository
git repack -a -d -q
mkdir "${WORK_DIR}/clean"
cp .git/objects/pack/pack-*.pack .git/objects/pack/pack-*.idx "${WORK_DIR}/clean/"
CLEAN_PACK="$(ls "${WORK_DIR}"/clean/pack-*.pack)"
CLEAN_IDX="${CLEAN_PACK%.pack}.idx"
PACK_OBJECTS="$(git verify-pack -v "${CLEAN_IDX}" | grep -cE '^[0-9a-f]{40} ')"
echo "Pack under test: ${PACK_OBJECTS} objects"

# Oldest commit's tree: the only tree of this history that mktree can rebuild from main.c alone
ROOT_TREE="$(git rev-parse "$(git rev-list --max-parents=0 HEAD)^{tree}")"

# salvage_case LABEL VICTIM_OID REPAIR_KIND
# Complement one byte inside VICTIM's pack entry, salvage with unpack-objects -r into an empty
# object store, check what survived, then rebuild VICTIM from outside the object database.
salvage_case() {
    local label="$1" victim="$2" kind="$3"
    local line vtype voffset pos orig new rc msg after_total after_ok loose missing_lines repaired tool

    # Start every case from the clean pack and an empty loose store
    rm -rf .git/objects/[0-9a-f][0-9a-f] .git/objects/pack/*
    cp "${WORK_DIR}"/clean/pack-* .git/objects/pack/

    line="$(git verify-pack -v "${CLEAN_IDX}" | grep "^${victim} ")"
    vtype="$(echo "${line}" | awk '{print $2}')"
    voffset="$(echo "${line}" | awk '{print $5}')"
    after_total="$(git verify-pack -v "${CLEAN_IDX}" | awk -v off="${voffset}" '/^[0-9a-f]{40} / && $5 > off' | wc -l | tr -d ' ')"

    cp "${CLEAN_PACK}" "${WORK_DIR}/damaged.pack"
    chmod u+w "${WORK_DIR}/damaged.pack"
    pos=$((voffset + 10))
    orig="$(od -An -tu1 -j "${pos}" -N1 "${WORK_DIR}/damaged.pack" | tr -d ' ')"
    new=$((orig ^ 255))
    # shellcheck disable=SC2059
    printf "$(printf '\\%03o' "${new}")" | dd of="${WORK_DIR}/damaged.pack" bs=1 seek="${pos}" count=1 conv=notrunc 2>/dev/null

    rm -f .git/objects/pack/*
    if git unpack-objects -r < "${WORK_DIR}/damaged.pack" >/dev/null 2>"${WORK_DIR}/unpack.err"; then
        rc=0
    else
        rc=$?
    fi
    msg="$(tail -n 1 "${WORK_DIR}/unpack.err")"
    echo "[${label}] unpack-objects -r exit status: ${rc} (${msg#fatal: })"
    if [ "${rc}" -ne 128 ]; then FAILED=1; fi

    loose="$(find .git/objects -type f -not -path ".git/objects/info/*" | wc -l | tr -d ' ')"
    echo "[${label}] Salvaged objects: ${loose} of ${PACK_OBJECTS}"

    after_ok=0
    while read -r oid; do
        if git cat-file -e "${oid}" 2>/dev/null; then after_ok=$((after_ok + 1)); fi
    done < <(git verify-pack -v "${CLEAN_IDX}" | awk -v off="${voffset}" '/^[0-9a-f]{40} / && $5 > off {print $1}')
    echo "[${label}] Entries after the damaged one that survived: ${after_ok} of ${after_total}"

    FSCK_SALVAGE="$(git fsck --full --no-dangling --no-progress 2>&1 || true)"
    echo "[${label}] fsck report:"
    echo "${FSCK_SALVAGE}" | grep -E '^(broken link|  +to|missing)'
    missing_lines="$(echo "${FSCK_SALVAGE}" | grep -c '^missing ' || true)"
    if [ "${missing_lines}" -eq 1 ] && echo "${FSCK_SALVAGE}" | grep -q "^missing ${vtype} ${victim}\$"; then
        echo "[${label}] Missing object identification: ISOLATED (missing ${vtype} ${victim})"
    else
        echo "[${label}] ERROR: fsck did not isolate exactly ${vtype} ${victim}"
        FAILED=1
    fi

    case "${kind}" in
        blob)
            tool="git hash-object"
            repaired="$(git hash-object -w engine.c)"
            ;;
        tree)
            tool="git mktree"
            repaired="$(printf '100644 blob %s\tmain.c\n' "${MAIN_BLOB}" | git mktree)"
            ;;
    esac
    if [ "${repaired}" = "${victim}" ]; then
        echo "[${label}] Rebuilt ${vtype} with ${tool}: MATCH"
    else
        echo "[${label}] Rebuilt ${vtype}: MISMATCH (${repaired} != ${victim})"
        FAILED=1
    fi

    if git fsck --full --no-dangling --no-progress >/dev/null 2>&1; then
        echo "[${label}] Final consistency check: REPAIRED (0 errors, graph fully connected)"
    else
        echo "[${label}] Final consistency check: FAILED"
        FAILED=1
    fi
}

salvage_case "mid-pack blob" "${BLOB_HASH}" blob
salvage_case "tail tree" "${ROOT_TREE}" tree

exit "${FAILED}"
