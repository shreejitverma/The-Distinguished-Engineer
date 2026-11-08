#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

"${CC:-gcc}" -O2 -Wall -Wextra -Werror -pedantic -std=c11 \
    "${SCRIPT_DIR}/signal_job_control.c" \
    -o "${WORKDIR}/signal_job_control"

"${WORKDIR}/signal_job_control"
