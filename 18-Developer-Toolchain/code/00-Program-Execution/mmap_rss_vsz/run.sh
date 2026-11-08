#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

"${CC:-cc}" -O2 -Wall -Wextra -std=c11 "${SCRIPT_DIR}/mmap_rss_vsz.c" -o "${WORKDIR}/mmap_rss_vsz"
"${WORKDIR}/mmap_rss_vsz"
