#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_REPO="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_REPO}"' EXIT

export GIT_AUTHOR_NAME="Toolchain Engineer"
export GIT_AUTHOR_EMAIL="engineer@example.com"
export GIT_COMMITTER_NAME="Forge Merge Queue"
export GIT_COMMITTER_EMAIL="queue@forge.internal"
export GIT_AUTHOR_DATE="2026-11-08T12:00:00Z"
export GIT_COMMITTER_DATE="2026-11-08T12:00:00Z"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C

python3 "${SCRIPT_DIR}/replay_engine.py" "${TMPDIR_REPO}"
