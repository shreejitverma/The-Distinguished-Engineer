#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${SCRIPT_DIR}/signal_job_control"

trap 'rm -f "${BIN}"' EXIT

"${CC:-gcc}" -O2 -Wall -Wextra -Werror -pedantic -std=c11 \
    "${SCRIPT_DIR}/signal_job_control.c" \
    -o "${BIN}"

exec "${BIN}"
