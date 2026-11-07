#!/usr/bin/env bash
set -euo pipefail

# Laboratory: SIMD Literal Search and Fast Text Search Mechanics
# Demonstrates:
# 1. Vectorized single-byte literal prefilter (memchr model)
# 2. Vectorized multi-pattern packed search (Slim Teddy 8-pattern model)
# 3. Rolling buffer stream scanner (grep-searcher model)
# 4. Byte-oriented bstr scanning avoiding UTF-8 validation overhead

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${SCRIPT_DIR}/simd_search"

trap 'rm -f "${BIN}"' EXIT

# Baseline x86-64 has no PSHUFB; the Teddy path needs SSSE3. AArch64 needs no extra flag.
SIMD_FLAGS=()
case "$(uname -m)" in
  x86_64|amd64) SIMD_FLAGS=(-mssse3) ;;
esac

"${CC:-cc}" -O3 -Wall -Wextra ${SIMD_FLAGS[@]+"${SIMD_FLAGS[@]}"} "${SCRIPT_DIR}/simd_search.c" -o "${BIN}"
"${BIN}"
