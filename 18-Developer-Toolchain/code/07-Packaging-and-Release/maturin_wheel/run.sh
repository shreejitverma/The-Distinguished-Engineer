#!/usr/bin/env bash
# requires: network
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# 1. Build the wheel with maturin in release mode (needs crates.io unless cached)
export CARGO_TERM_COLOR=never
maturin build --release --out target/wheels >/dev/null 2>&1

# 2. Deterministic verification of the built wheel archive and binary
python3 "${SCRIPT_DIR}/verify_wheel.py" "${SCRIPT_DIR}/target/wheels"

# Clean up unpacked artifacts
rm -rf "${SCRIPT_DIR}/target/wheels/unpacked"
