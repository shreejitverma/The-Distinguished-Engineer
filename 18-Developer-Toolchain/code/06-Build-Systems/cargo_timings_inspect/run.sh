#!/usr/bin/env bash
# Cargo build graph lab: rmeta pipelining, feature unification, fingerprint
# hashing, build script rerun rules, and the C and C++ linker handoff.
# No harness requirements: it runs on macOS as is with stable cargo, a C and C++
# compiler, ar, and python3, and uses path dependencies only, so it needs no network.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for tool in cargo rustc python3 ar "${CC:-cc}" "${CXX:-c++}"; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        echo "missing required tool: ${tool}" >&2
        exit 2
    fi
done

SCRATCH="$(mktemp -d)"
trap 'rm -rf "${SCRATCH}"' EXIT

export LC_ALL=C
export CC="${CC:-cc}"
export CXX="${CXX:-c++}"

python3 "${SCRIPT_DIR}/inspect_build.py" "${SCRATCH}"
