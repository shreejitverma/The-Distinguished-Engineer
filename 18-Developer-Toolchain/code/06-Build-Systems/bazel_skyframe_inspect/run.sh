#!/usr/bin/env bash
# requires: network
#
# Bazel 9.3.2 lab: Bzlmod MVS over a local file:// registry, query vs cquery vs aquery,
# providers and an aspect, a JSON persistent worker, and what each kind of edit re-runs.
# Needs bazelisk (or bazel 9.3.2) and python3 on PATH. The network is used only to resolve
# Bazel's own built-in module dependencies from bcr.bazel.build on a cold repository cache.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRATCH="$(mktemp -d)"
cleanup() {
    if [ -d "${SCRATCH}/ws" ]; then
        (cd "${SCRATCH}/ws" && bazel --ignore_all_rc_files --output_base="${SCRATCH}/ob" shutdown >/dev/null 2>&1) || true
    fi
    chmod -R u+w "${SCRATCH}" 2>/dev/null || true
    rm -rf "${SCRATCH}"
}
trap cleanup EXIT

export LC_ALL=C
export BAZELISK_SKIP_WRAPPER=1
# The workspace pins 9.3.2 in .bazelversion; bazelisk honors it.
python3 -I "${SCRIPT_DIR}/drive_bazel.py" "${SCRIPT_DIR}" "${SCRATCH}"
