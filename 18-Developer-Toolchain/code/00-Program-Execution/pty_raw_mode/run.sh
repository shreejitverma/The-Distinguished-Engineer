#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

"${CC:-cc}" -Wall -Wextra -O2 "${SCRIPT_DIR}/pty_raw_mode.c" -o "${WORKDIR}/pty_raw_mode"
"${WORKDIR}/pty_raw_mode"
