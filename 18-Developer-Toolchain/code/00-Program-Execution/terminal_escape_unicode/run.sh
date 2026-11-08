#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

"${CC:-cc}" -O2 -Wall -Wextra -pedantic -std=c99 "${SCRIPT_DIR}/terminal_engine.c" -o "${WORKDIR}/terminal_engine"
"${WORKDIR}/terminal_engine"
