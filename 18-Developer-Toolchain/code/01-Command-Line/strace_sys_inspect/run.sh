#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

export LC_ALL=C

CC="${CC:-gcc}"
"$CC" -std=c11 -Wall -Wextra -O2 -o "${WORKDIR}/lab" "${SCRIPT_DIR}/main.c"
"${WORKDIR}/lab"
