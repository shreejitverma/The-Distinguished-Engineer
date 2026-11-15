#!/usr/bin/env bash
# Remote Execution API v2 lab: canonical encoding, Merkle input roots, CAS/AC/Execution/ByteStream
# semantics, Build without the Bytes, cache poisoning, SHA256TREE, and chunking.
# Needs only python3 (3.9+); Bazel is not run here. fixtures/bazel/ holds real Bazel 9.3.2
# disk-cache entries recorded by capture_bazel_fixture.sh, which needs network and Bazel.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1
cd "${SCRIPT_DIR}"
exec python3 -I -B -c 'import sys; sys.path.insert(0, "."); import demo; sys.exit(demo.main())'
