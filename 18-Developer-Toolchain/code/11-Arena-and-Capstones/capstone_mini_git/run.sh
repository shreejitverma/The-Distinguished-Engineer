#!/usr/bin/env bash
# No requirement header: cargo builds offline (no crate dependencies); git, cargo, and libz come from the host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export GIT_AUTHOR_NAME="Distinguished Engineer"
export GIT_AUTHOR_EMAIL="engineer@example.com"
export GIT_COMMITTER_NAME="Distinguished Committer"
export GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_AUTHOR_DATE="2026-11-26T12:00:00Z"
export GIT_COMMITTER_DATE="2026-11-26T12:00:00Z"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null

TMP_SHA1="$(mktemp -d)"
TMP_SHA256="$(mktemp -d)"
trap 'rm -rf "${TMP_SHA1}" "${TMP_SHA256}"' EXIT

cd "${TMP_SHA1}"
git init -b main -q
echo "Hello from root" > README.md
mkdir -p src
for i in $(seq 1 40); do
    echo "Line $i of source implementation buffer with deterministic content" >> src/lib.rs
done
git add README.md src/lib.rs
git commit -m "Initial commit in SHA-1 repo" -q

for rev in 2 3 4; do
    export GIT_AUTHOR_DATE="2026-11-26T12:0${rev}:00Z"
    export GIT_COMMITTER_DATE="2026-11-26T12:0${rev}:00Z"
    echo "Line modified in revision $rev with appended bytes" >> src/lib.rs
    git commit -am "Revision $rev delta build" -q
done

# Write commit-graph and repack into packfile with delta compression
git commit-graph write --reachable --no-progress
git repack -ad -q

cd "${TMP_SHA256}"
git init -b main --object-format=sha256 -q
echo "SHA-256 root payload" > test256.txt
git add test256.txt
git commit -m "Initial commit in SHA-256 repo" -q

cd "${SCRIPT_DIR}/minigit_rs"
cargo build --quiet --release

"${SCRIPT_DIR}/minigit_rs/target/release/minigit_rs" verify-all "${TMP_SHA1}" "${TMP_SHA256}"
