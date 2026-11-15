#!/usr/bin/env bash
# A stand-in "emulator": it records that CTest routed the test through it, then runs the binary natively.
set -euo pipefail
[[ "${1:-}" == "--quiet" ]] && shift
echo "emu.sh ran $(basename "$1")" >> "${LAB_EMU_LOG:?LAB_EMU_LOG must be set}"
exec "$@"
