#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

"${CC:-cc}" -Wall -Wextra -O2 pty_raw_mode.c -o pty_raw_mode
./pty_raw_mode
rm -f pty_raw_mode
