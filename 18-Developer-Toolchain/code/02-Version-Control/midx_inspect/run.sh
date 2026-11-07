#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_REPO="$(mktemp -d)"
trap 'chmod -R 0777 "${TMPDIR_REPO}" 2>/dev/null || true; rm -rf "${TMPDIR_REPO}"' EXIT

export GIT_AUTHOR_NAME="Toolchain Author"
export GIT_AUTHOR_EMAIL="author@example.com"
export GIT_COMMITTER_NAME="Toolchain Committer"
export GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

cd "${TMPDIR_REPO}"
git init -b main -q
git config gc.auto 0

# 1. Commit 1: Initial core engine
export GIT_AUTHOR_DATE="2026-10-25T12:00:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:00:00Z"
echo "core system v1" > engine.c
git add engine.c
git commit -m "c1: initial core engine" -q
git rev-list --objects HEAD | git -c pack.threads=1 pack-objects .git/objects/pack/pack -q >/dev/null
git prune-packed

# 2. Commit 2: Update engine and add config parser
export GIT_AUTHOR_DATE="2026-10-25T12:01:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:01:00Z"
echo "core system v2" >> engine.c
echo "config parser v1" > config.c
git add engine.c config.c
git commit -m "c2: add config parser and update engine" -q
git rev-list --objects HEAD ^HEAD~1 | git -c pack.threads=1 pack-objects .git/objects/pack/pack -q >/dev/null
git prune-packed

# 3. Commit 3: Feature branch commit
git checkout -b feature -q
export GIT_AUTHOR_DATE="2026-10-25T12:02:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:02:00Z"
echo "feature logic" > feature.c
git add feature.c
git commit -m "c3: feature branch commit" -q
git rev-list --objects HEAD ^main | git -c pack.threads=1 pack-objects .git/objects/pack/pack -q >/dev/null
git prune-packed

# 4. Commit 4: Telemetry on main
git checkout main -q
export GIT_AUTHOR_DATE="2026-10-25T12:03:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:03:00Z"
echo "telemetry instrumentation" > telemetry.c
git add telemetry.c
git commit -m "c4: telemetry commit" -q
git rev-list --objects HEAD ^main~1 | git -c pack.threads=1 pack-objects .git/objects/pack/pack -q >/dev/null
git prune-packed

# 5. Commit 5: Merge commit
export GIT_AUTHOR_DATE="2026-10-25T12:04:00Z"
export GIT_COMMITTER_DATE="2026-10-25T12:04:00Z"
git merge feature -m "c5: merge feature into main" -q
git rev-list --objects HEAD ^HEAD~1 ^feature | git -c pack.threads=1 pack-objects .git/objects/pack/pack -q >/dev/null
git prune-packed

# Identify deterministic preferred pack for bitmap generation
PREF_PACK="$(basename "$(ls .git/objects/pack/*.idx | sort | head -n 1)")"

# Generate Multi-Pack-Index with reachability bitmap
git multi-pack-index write --no-progress --bitmap --preferred-pack="${PREF_PACK}"

# Generate Commit-Graph with changed-paths Bloom filters
git commit-graph write --no-progress --reachable --changed-paths

# Inspect and verify binary structures
python3 "${SCRIPT_DIR}/midx_inspect.py" "${TMPDIR_REPO}" "${PREF_PACK}"
