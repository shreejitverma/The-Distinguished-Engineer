#!/usr/bin/env bash
# Large-scale change lab: an AST codemod campaign over a synthetic six-repository fleet.
# Needs only git and python3 (3.9 or newer); no network.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export GIT_AUTHOR_NAME="Toolchain Author"
export GIT_AUTHOR_EMAIL="author@example.com"
export GIT_COMMITTER_NAME="Toolchain Committer"
export GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_AUTHOR_DATE="2026-11-21T12:00:00Z"
export GIT_COMMITTER_DATE="2026-11-21T12:00:00Z"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

python3 "${SCRIPT_DIR}/campaign.py"
