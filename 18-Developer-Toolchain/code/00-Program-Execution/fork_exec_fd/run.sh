#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

"${CC:-cc}" -O2 -Wall -Wextra -pthread -o "${WORKDIR}/fork_exec_fd" "${SCRIPT_DIR}/fork_exec_fd.c"

cd "${WORKDIR}"
"${WORKDIR}/fork_exec_fd"
