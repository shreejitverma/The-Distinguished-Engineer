#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_REPO="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_REPO}"' EXIT

python3 "${SCRIPT_DIR}/demo.py" "${TMPDIR_REPO}"
