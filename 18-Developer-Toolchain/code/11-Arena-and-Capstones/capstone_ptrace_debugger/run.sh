#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_LAB="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_LAB}"' EXIT

cd "${TMPDIR_LAB}"
cp "${SCRIPT_DIR}/target.c" .
cp "${SCRIPT_DIR}/debugger.c" .
cp "${SCRIPT_DIR}/Makefile" .

make -s all

# Run debugger targeting line 7 (the entry of target_function)
./debugger ./target 7
