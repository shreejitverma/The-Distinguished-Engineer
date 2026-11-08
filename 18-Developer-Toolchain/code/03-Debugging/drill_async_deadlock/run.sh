#!/usr/bin/env bash
# Drill: diagnose asyncio and Rust executor hangs live, without restarting the process.
# Part 1 (drive.py + service.py): four asyncio hangs inspected with SIGUSR1 (loop-level
#   task dump via asyncio.capture_call_graph) and SIGUSR2 (faulthandler thread dump).
# Part 2 (rust/): a std-only executor that counts waker clones and drops per task and
#   prints a tokio-console-style lost-waker audit; no crates, so it builds offline.
# Needs python3 >= 3.14 and cargo, and no requirement tag: it runs on macOS and Linux as is.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

echo "##### Part 1: Python asyncio #####"
python3 -I "${SCRIPT_DIR}/drive.py"

echo
echo "##### Part 2: Rust waker audit #####"
# Build from a copy so Cargo.lock and target/ never land in the lab directory.
cp -R "${SCRIPT_DIR}/rust" "${WORK}/rust"
CARGO_TARGET_DIR="${WORK}/target" cargo build --release --quiet --offline --manifest-path "${WORK}/rust/Cargo.toml"
"${WORK}/target/release/waker_audit"
