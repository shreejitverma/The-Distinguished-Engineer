#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

export LC_ALL=C

CC="${CC:-gcc}"
"$CC" -std=c11 -Wall -Wextra -O2 -o lab main.c
./lab
