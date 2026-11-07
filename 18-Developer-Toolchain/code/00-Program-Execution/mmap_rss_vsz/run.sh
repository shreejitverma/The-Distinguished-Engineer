#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

"${CC:-cc}" -O2 -Wall -Wextra -std=c11 mmap_rss_vsz.c -o mmap_rss_vsz
./mmap_rss_vsz
