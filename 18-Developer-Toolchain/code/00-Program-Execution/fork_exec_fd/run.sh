#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${SCRIPT_DIR}/fork_exec_fd"

trap 'rm -f "${BIN}"' EXIT

"${CC:-cc}" -O2 -Wall -Wextra -pthread -o "${BIN}" "${SCRIPT_DIR}/fork_exec_fd.c"

cd "${SCRIPT_DIR}"
exec "${BIN}"
